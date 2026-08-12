"""Bind remaps planner-invented component references onto reviewed authority.

The planner authors topology against the redacted reviewed context. When it
invents its own output name anyway, ``bind_guided_reviewed_components`` must
restore the reviewed name AND rewrite every reference to the invented name —
source/node ``on_success``/``on_error`` routing, gate ``routes``/``fork_to``
targets, and the ``edges`` array — or the candidate dies at set_pipeline
validation with "unknown node" and the repair loop burns to REPAIR_EXHAUSTED
(elspeth-859e2702dd, elspeth-2e8a711248).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.guided import planning as guided_planning
from elspeth.web.composer.guided.planning import (
    GuidedCandidateBindingRejected,
    GuidedCorrectionTarget,
    GuidedEdgeRoutingAuthority,
    GuidedRevisionAuthority,
    bind_guided_reviewed_components,
    guided_authorized_pipeline_schema,
    materialize_guided_authorized_candidate,
)
from elspeth.web.composer.guided.protocol import GuidedStep
from elspeth.web.composer.guided.resolved import SinkOutputResolved, SourceResolved
from elspeth.web.composer.guided.state_machine import ComponentTarget, GuidedSession
from elspeth.web.composer.pipeline_planner import _CANDIDATE_SHAPE_INTEGRITY_PREFIX
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools import ToolContext, build_set_pipeline_candidate
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

SOURCE_ID = "11111111-1111-4111-8111-111111111111"
OUTPUT_ID = "33333333-3333-4333-8333-333333333333"


def _guided() -> GuidedSession:
    return GuidedSession(
        step=GuidedStep.STEP_3_TRANSFORMS,
        source_order=(SOURCE_ID,),
        reviewed_sources={
            SOURCE_ID: SourceResolved(
                name="source",
                plugin="csv",
                options={"path": "blob:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
                observed_columns=("color_name", "hex"),
                sample_rows=(),
                on_validation_failure="discard",
            )
        },
        output_order=(OUTPUT_ID,),
        reviewed_outputs={
            OUTPUT_ID: SinkOutputResolved(
                name="output",
                plugin="json",
                options={"path": "outputs/colours.json"},
                required_fields=(),
                schema_mode="observed",
                on_write_failure="discard",
            )
        },
    )


def test_bind_still_refuses_a_candidate_that_names_no_source() -> None:
    """The binder's contract is unchanged; only the planner loop's handling is.

    Repairing a sources-free candidate is the planner loop's job
    (elspeth-bcc6bdac99) — it rejects the shape ahead of the finalizer. The
    binder must keep refusing outright: it has no reviewed component to bind
    and must never invent one.
    """
    pipeline = {
        "nodes": [],
        "edges": [],
        "outputs": [
            {"sink_name": "output", "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }

    with pytest.raises(AuditIntegrityError, match="does not identify reviewed sources") as raised:
        bind_guided_reviewed_components(pipeline, _guided())

    # The planner loop reclassifies binder candidate-shape complaints by this
    # message prefix; pin the real message against the real constant so the
    # two sides cannot drift into a terminal 500 again.
    assert str(raised.value).startswith(_CANDIDATE_SHAPE_INTEGRITY_PREFIX)


def test_bind_rewrites_invented_output_name_in_edges_and_routing() -> None:
    # The planner invented "colours_json" for the reviewed output and wired
    # both the source routing and an edges[] entry to it.
    pipeline = {
        "sources": {
            "source": {
                "plugin": "csv",
                "options": {},
                "on_success": "colours_json",
                "on_validation_failure": "discard",
            }
        },
        "nodes": [],
        "edges": [
            {"id": "e1", "from_node": "source", "to_node": "colours_json", "edge_type": "on_success"},
        ],
        "outputs": [
            {"sink_name": "colours_json", "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }

    bound = bind_guided_reviewed_components(pipeline, _guided())

    assert [output["sink_name"] for output in bound["outputs"]] == ["output"]
    assert bound["sources"]["source"]["on_success"] == "output"
    assert bound["edges"] == [
        {"id": "e1", "from_node": "source", "to_node": "output", "edge_type": "on_success"},
    ]


def test_initial_guided_delta_schema_excludes_reviewed_configuration_authority() -> None:
    schema = guided_authorized_pipeline_schema(_guided(), correction_target=None)

    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"source_routes", "nodes", "edges", "output_targets", "metadata"}
    source_item = schema["properties"]["source_routes"]["items"]
    output_item = schema["properties"]["output_targets"]["items"]
    assert set(source_item["properties"]) == {"stable_id", "on_success"}
    assert set(output_item["properties"]) == {"stable_id"}
    assert source_item["additionalProperties"] is False
    assert output_item["additionalProperties"] is False
    assert schema["properties"]["nodes"]["items"]["additionalProperties"] is False
    assert schema["properties"]["edges"]["items"]["additionalProperties"] is False
    serialized = repr(schema)
    assert "blob_id" not in serialized
    assert "inline_blob" not in serialized
    assert "on_validation_failure" not in serialized
    assert "on_write_failure" not in serialized
    assert "outputs/colours.json" not in serialized

    validator = Draft202012Validator(schema)
    base = {
        "source_routes": [{"stable_id": SOURCE_ID, "on_success": "output"}],
        "nodes": [],
        "edges": [],
        "output_targets": [{"stable_id": OUTPUT_ID}],
    }
    adversarial = (
        {**base, "source_routes": [{**base["source_routes"][0], "plugin": "json"}]},
        {**base, "source_routes": [{**base["source_routes"][0], "options": {"path": "/tmp/replaced"}}]},
        {**base, "source_routes": [{**base["source_routes"][0], "blob_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}]},
        {**base, "output_targets": [{**base["output_targets"][0], "plugin": "csv"}]},
        {**base, "output_targets": [{**base["output_targets"][0], "options": {"schema": {"required_fields": []}}}]},
        {**base, "output_targets": [{**base["output_targets"][0], "on_write_failure": None}]},
    )
    assert all(list(validator.iter_errors(candidate)) for candidate in adversarial)


def test_edge_correction_schema_exposes_only_one_selected_routing_patch() -> None:
    target = GuidedCorrectionTarget(
        requested=ComponentTarget(kind="edge", stable_id="77777777-7777-4777-8777-777777777777"),
        owner_kind="node",
        owner_key="amount_gate",
        authority_key=None,
        public_target={
            "stable_id": "77777777-7777-4777-8777-777777777777",
            "from_endpoint": {"kind": "node", "stable_id": "66666666-6666-4666-8666-666666666666"},
            "to_endpoint": {"kind": "node", "stable_id": "55555555-5555-4555-8555-555555555555"},
            "flow": {"kind": "gate_route", "route": "route-1", "branch": None},
        },
        before_fingerprint="3" * 64,
    )

    schema = guided_authorized_pipeline_schema(_guided(), correction_target=target)

    assert set(schema["properties"]) == {"edge_patch"}
    patch_schema = schema["properties"]["edge_patch"]
    assert set(patch_schema["properties"]) == {"stable_id", "to_node"}
    assert patch_schema["properties"]["stable_id"]["const"] == target.requested.stable_id
    validator = Draft202012Validator(schema)
    valid = {"edge_patch": {"stable_id": target.requested.stable_id, "to_node": "revised_rows"}}
    assert not list(validator.iter_errors(valid))
    assert list(validator.iter_errors({**valid, "nodes": []}))
    assert list(
        validator.iter_errors(
            {
                "edge_patch": {
                    **valid["edge_patch"],
                    "condition": "True",
                    "routes": {"false": "discard"},
                }
            }
        )
    )


def test_edge_correction_materializer_reroutes_only_the_selected_gate_route() -> None:
    predecessor = _correction_predecessor()
    edge_id = "77777777-7777-4777-8777-777777777777"
    target = GuidedCorrectionTarget(
        requested=ComponentTarget(kind="edge", stable_id=edge_id),
        owner_kind="node",
        owner_key="amount_gate",
        authority_key=None,
        public_target={
            "stable_id": edge_id,
            "from_endpoint": {"kind": "node", "stable_id": "66666666-6666-4666-8666-666666666666"},
            "to_endpoint": {"kind": "node", "stable_id": "55555555-5555-4555-8555-555555555555"},
            "flow": {"kind": "gate_route", "route": "route-1", "branch": None},
        },
        before_fingerprint="3" * 64,
        edge_routing=GuidedEdgeRoutingAuthority(
            field="routes",
            route_key="true",
            fork_index=None,
            before_destination="high_value",
        ),
    )

    bound = materialize_guided_authorized_candidate(
        {"edge_patch": {"stable_id": edge_id, "to_node": "standard"}},
        authority=target,
        guided=_guided(),
        current_state=predecessor,
    )

    before = predecessor.to_dict()["nodes"][0]
    assert bound["nodes"][0]["routes"] == {"true": "standard", "false": "standard"}
    assert bound["nodes"][0]["condition"] == before["condition"]
    assert bound["nodes"][0].get("fork_to") == before.get("fork_to")
    assert bound["nodes"][0]["options"] == before["options"]
    assert bound["nodes"][1:] == predecessor.to_dict()["nodes"][1:]


def test_node_correction_schema_exposes_a_public_patch_not_a_canonical_node() -> None:
    schema = guided_authorized_pipeline_schema(_guided(), correction_target=_format_node_correction_target())

    assert set(schema["properties"]) == {"node_patch", "edges"}
    patch_schema = schema["properties"]["node_patch"]
    assert patch_schema["properties"]["stable_id"]["const"] == _format_node_correction_target().requested.stable_id
    assert "id" not in patch_schema["properties"]
    assert "node_type" not in patch_schema["properties"]
    assert "plugin" not in patch_schema["properties"]


def test_initial_guided_delta_materializes_reviewed_authority_server_side() -> None:
    delta = {
        "source_routes": [{"stable_id": SOURCE_ID, "on_success": "clean_rows"}],
        "nodes": [
            {
                "id": "clean_rows",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "clean_rows",
                "on_success": "output",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            }
        ],
        "edges": [],
        "output_targets": [{"stable_id": OUTPUT_ID}],
        "metadata": {"name": "Generic linear pipeline"},
    }

    bound = materialize_guided_authorized_candidate(
        delta,
        authority=None,
        guided=_guided(),
        current_state=CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        ),
    )

    assert bound["sources"] == {
        "source": {
            "plugin": "csv",
            "options": {"path": "blob:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
            "on_success": "clean_rows",
            "on_validation_failure": "discard",
        }
    }
    assert bound["outputs"] == [
        {
            "sink_name": "output",
            "plugin": "json",
            "options": {"path": "outputs/colours.json"},
            "on_write_failure": "discard",
        }
    ]
    assert bound["nodes"] == delta["nodes"]


def test_guided_delta_cannot_evade_reviewed_required_fields() -> None:
    guided = _guided_with_output(
        required_fields=("document_uri", "abstract"),
        output_options={"path": "outputs/generic.jsonl", "schema": {"mode": "observed"}},
    )
    bound = materialize_guided_authorized_candidate(
        {
            "source_routes": [{"stable_id": SOURCE_ID, "on_success": "output"}],
            "nodes": [],
            "edges": [],
            "output_targets": [{"stable_id": OUTPUT_ID}],
        },
        authority=None,
        guided=guided,
        current_state=CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        ),
    )

    assert bound["outputs"][0]["options"]["schema"]["required_fields"] == ["document_uri", "abstract"]


@pytest.mark.parametrize(
    ("source_routes", "output_targets", "expected_code"),
    (
        (
            [{"stable_id": "99999999-9999-4999-8999-999999999999", "on_success": "output"}],
            [{"stable_id": OUTPUT_ID}],
            "guided_delta_unknown_stable_id",
        ),
        (
            [{"stable_id": SOURCE_ID, "on_success": "output"}, {"stable_id": SOURCE_ID, "on_success": "output"}],
            [{"stable_id": OUTPUT_ID}],
            "guided_delta_duplicate_stable_id",
        ),
        (
            [{"stable_id": SOURCE_ID, "on_success": "output"}],
            [{"stable_id": OUTPUT_ID}, {"stable_id": OUTPUT_ID}],
            "guided_delta_duplicate_stable_id",
        ),
    ),
)
def test_guided_delta_rejects_unknown_or_duplicate_stable_ids(
    source_routes: list[dict[str, str]],
    output_targets: list[dict[str, str]],
    expected_code: str,
) -> None:
    with pytest.raises(GuidedCandidateBindingRejected) as raised:
        materialize_guided_authorized_candidate(
            {
                "source_routes": source_routes,
                "nodes": [],
                "edges": [],
                "output_targets": output_targets,
            },
            authority=None,
            guided=_guided(),
            current_state=CompositionState(
                source=None,
                nodes=(),
                edges=(),
                outputs=(),
                metadata=PipelineMetadata(),
                version=1,
            ),
        )

    assert raised.value.error_code == expected_code


def test_bind_rewrites_invented_output_name_in_gate_routes() -> None:
    # Gate routes target sinks BY NAME: the DAG builder resolves each route
    # value against sink_ids before deferring to connection names, so an
    # invented output name in routes{} is a live sink reference that must be
    # remapped exactly like on_success/on_error (elspeth-2e8a711248).
    pipeline = {
        "sources": {
            "source": {
                "plugin": "csv",
                "options": {},
                "on_success": "hex_gate",
                "on_validation_failure": "discard",
            }
        },
        "nodes": [
            {
                "id": "hex_gate",
                "node_type": "gate",
                "input": "hex_gate",
                "condition": "row['hex'] != ''",
                "routes": {"true": "colours_json", "false": "discard"},
            }
        ],
        "edges": [],
        "outputs": [
            {"sink_name": "colours_json", "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }

    bound = bind_guided_reviewed_components(pipeline, _guided())

    assert bound["nodes"][0]["routes"] == {"true": "output", "false": "discard"}


def test_bind_rewrites_invented_output_name_in_fork_to() -> None:
    # fork_to entries are also resolved against sink names first ("Explicit
    # sink destination") — a fork branch aimed at the invented output name
    # must be renamed; sibling branch names and the "fork" route sentinel
    # must pass through untouched.
    pipeline = {
        "sources": {
            "source": {
                "plugin": "csv",
                "options": {},
                "on_success": "fan_out",
                "on_validation_failure": "discard",
            }
        },
        "nodes": [
            {
                "id": "fan_out",
                "node_type": "gate",
                "input": "fan_out",
                "condition": "True",
                "routes": {"true": "fork", "false": "fork"},
                "fork_to": ["colours_json", "audit_branch"],
            },
            {
                "id": "audit",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "audit_branch",
                "on_success": "colours_json",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
        ],
        "edges": [],
        "outputs": [
            {"sink_name": "colours_json", "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }

    bound = bind_guided_reviewed_components(pipeline, _guided())

    nodes_by_id = {node["id"]: node for node in bound["nodes"]}
    assert nodes_by_id["fan_out"]["fork_to"] == ["output", "audit_branch"]
    assert nodes_by_id["fan_out"]["routes"] == {"true": "fork", "false": "fork"}
    assert nodes_by_id["audit"]["on_success"] == "output"


def test_candidate_state_defaults_missing_edge_label() -> None:
    # set_pipeline's tool schema makes edges[*].label optional and the handler
    # reads it with .get(); the proposal round-trip must apply the same
    # default before the canonical EdgeSpec.from_dict (which is strict).
    from elspeth.core.canonical import stable_hash
    from elspeth.web.composer.guided.planning import guided_candidate_state
    from elspeth.web.composer.pipeline_proposal import AbsentBase, PipelineProposal, PlannerSurface

    proposal = PipelineProposal.create(
        pipeline={
            "sources": {
                "source": {
                    "plugin": "csv",
                    "options": {},
                    "on_success": "output",
                    "on_validation_failure": "discard",
                }
            },
            "nodes": [],
            "edges": [{"id": "e1", "from_node": "source", "to_node": "output", "edge_type": "on_success"}],
            "outputs": [{"sink_name": "output", "plugin": "json", "options": {}, "on_write_failure": "discard"}],
        },
        base=AbsentBase(),
        reviewed_facts={},
        surface=PlannerSurface.GUIDED_STAGED,
        repair_count=0,
        skill_hash=stable_hash("edge-label-default-test"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=None,
    )

    state = guided_candidate_state(proposal)

    assert state.edges[0].label is None


def test_bind_rejects_unproven_dangling_sink_reference_with_repair_facts() -> None:
    # Planner slip observed live: outputs and edges correctly use the reviewed
    # name, but one stale invented name ('csv_rows') survives in on_success.
    # The rename map is empty, so nothing proves 'csv_rows' aliases the sink —
    # the binder must NOT guess (elspeth-572c642dbf: the old unknown->sink
    # rewrite converted invalid plans into valid-but-different pipelines).
    # Instead it rejects with the facts a one-turn repair needs: the dangling
    # value the planner authored plus the valid destination vocabulary.
    # Validation cannot carry those facts here: the binder always rewrites
    # source/output CONFIG, so source-attributed entries project config-masked
    # (no connectivity facts) and the repair would be blind.
    pipeline = {
        "sources": {
            "source": {
                "plugin": "csv",
                "options": {},
                "on_success": "csv_rows",
                "on_validation_failure": "discard",
            }
        },
        "nodes": [],
        "edges": [{"id": "e1", "from_node": "source", "to_node": "output", "edge_type": "on_success"}],
        "outputs": [
            {"sink_name": "output", "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }

    with pytest.raises(GuidedCandidateBindingRejected) as raised:
        bind_guided_reviewed_components(pipeline, _guided())

    assert str(raised.value).startswith(_CANDIDATE_SHAPE_INTEGRITY_PREFIX)
    assert raised.value.error_code == "guided_route_target_unknown"
    assert raised.value.connectivity == {
        "dangling_references": ["csv_rows"],
        "declared_sinks": ["output"],
        "consumable_connections": [],
    }


def test_bind_rejects_unknown_edge_destination() -> None:
    # Only the destination can be a sink reference; a to_node outside every
    # known target is the edge twin of the dangling on_success above.
    pipeline = {
        "sources": {
            "source": {
                "plugin": "csv",
                "options": {},
                "on_success": "output",
                "on_validation_failure": "discard",
            }
        },
        "nodes": [],
        "edges": [{"id": "e1", "from_node": "source", "to_node": "nowhere", "edge_type": "on_success"}],
        "outputs": [
            {"sink_name": "output", "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }

    with pytest.raises(GuidedCandidateBindingRejected) as raised:
        bind_guided_reviewed_components(pipeline, _guided())

    assert raised.value.error_code == "guided_route_target_unknown"
    assert raised.value.connectivity["dangling_references"] == ["nowhere"]


def test_bind_leaves_unknown_edge_origin_for_validation() -> None:
    # A dangling from_node has no sink resolution — it is never a sink
    # reference, so the binder neither rewrites nor rejects it; validation
    # owns that defect with its own node-attributed (disclosed) code.
    pipeline = {
        "sources": {
            "source": {
                "plugin": "csv",
                "options": {},
                "on_success": "output",
                "on_validation_failure": "discard",
            }
        },
        "nodes": [],
        "edges": [{"id": "e1", "from_node": "ghost", "to_node": "output", "edge_type": "on_success"}],
        "outputs": [
            {"sink_name": "output", "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }

    bound = bind_guided_reviewed_components(pipeline, _guided())

    assert bound["edges"] == [{"id": "e1", "from_node": "ghost", "to_node": "output", "edge_type": "on_success"}]


OUTPUT_B_ID = "44444444-4444-4444-8444-444444444444"


def _guided_two_outputs() -> GuidedSession:
    base = _guided()
    return GuidedSession(
        step=base.step,
        source_order=base.source_order,
        reviewed_sources=dict(base.reviewed_sources),
        output_order=(OUTPUT_ID, OUTPUT_B_ID),
        reviewed_outputs={
            OUTPUT_ID: base.reviewed_outputs[OUTPUT_ID],
            OUTPUT_B_ID: SinkOutputResolved(
                name="quarantine",
                plugin="json",
                options={"path": "outputs/quarantine.json"},
                required_fields=(),
                schema_mode="observed",
                on_write_failure="discard",
            ),
        },
    )


def _two_output_candidate(first_alias: str, second_alias: str) -> dict[str, object]:
    return {
        "sources": {
            "source": {
                "plugin": "csv",
                "options": {},
                "on_success": "triage",
                "on_validation_failure": "discard",
            }
        },
        "nodes": [
            {
                "id": "triage",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "triage",
                "on_success": first_alias,
                "on_error": second_alias,
                "options": {"schema": {"mode": "observed"}},
            }
        ],
        "edges": [],
        "outputs": [
            {"sink_name": first_alias, "plugin": "json", "options": {}, "on_write_failure": "discard"},
            {"sink_name": second_alias, "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }


def test_bind_multi_output_distinct_aliases_rewrite_each_reference() -> None:
    # The legal multi-output shape: each candidate output records its own
    # alias, and every reference follows its alias to the matching reviewed
    # name — no collapse onto one sink (elspeth-572c642dbf).
    bound = bind_guided_reviewed_components(_two_output_candidate("main_out", "spill_out"), _guided_two_outputs())

    assert [output["sink_name"] for output in bound["outputs"]] == ["output", "quarantine"]
    node = bound["nodes"][0]
    assert node["on_success"] == "output"
    assert node["on_error"] == "quarantine"


def test_bind_rejects_duplicate_output_aliases_before_any_rewrite() -> None:
    # Two candidate outputs sharing one alias made the old dict last-write-
    # wins: every 'result' reference silently retargeted the SECOND reviewed
    # sink (elspeth-572c642dbf). Ambiguous aliasing must be a typed repairable
    # rejection, never a rewrite.
    with pytest.raises(GuidedCandidateBindingRejected) as raised:
        bind_guided_reviewed_components(_two_output_candidate("result", "result"), _guided_two_outputs())

    assert str(raised.value).startswith(_CANDIDATE_SHAPE_INTEGRITY_PREFIX)
    assert raised.value.error_code == "guided_output_alias_collision"
    assert raised.value.connectivity == {"colliding_aliases": ["result"]}


def test_bind_rejects_alias_reusing_a_sibling_reviewed_output_name() -> None:
    # Candidate output 0 aliases itself as reviewed output 1's NAME: the
    # rename map would rewrite references legitimately targeting output 1
    # onto output 0 — indistinguishable intents, so reject.
    with pytest.raises(GuidedCandidateBindingRejected) as raised:
        bind_guided_reviewed_components(_two_output_candidate("quarantine", "errors_out"), _guided_two_outputs())

    assert raised.value.error_code == "guided_output_alias_collision"
    assert raised.value.connectivity == {"colliding_aliases": ["quarantine"]}


def test_bind_rejects_alias_colliding_with_node_or_connection_names() -> None:
    # An alias that is also a node id (or a consumed connection) makes the
    # rename pass rewrite topology references that never meant the sink.
    pipeline = {
        "sources": {
            "source": {
                "plugin": "csv",
                "options": {},
                "on_success": "clean",
                "on_validation_failure": "discard",
            }
        },
        "nodes": [
            {
                "id": "clean",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "clean",
                "on_success": "clean_rows",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            }
        ],
        "edges": [],
        "outputs": [
            {"sink_name": "clean", "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }

    with pytest.raises(GuidedCandidateBindingRejected) as raised:
        bind_guided_reviewed_components(pipeline, _guided())

    assert raised.value.error_code == "guided_output_alias_collision"
    assert raised.value.connectivity == {"colliding_aliases": ["clean"]}


def test_bind_rejects_node_id_shadowing_a_reviewed_sink_name() -> None:
    # The planner never sees reviewed sink names, so it can innocently name a
    # transform node "output". After the binder restores the reviewed name,
    # the DAG builder resolves route targets against sink names BEFORE
    # connection/node names, so the source's on_success meant for the node
    # would silently deliver rows to the sink and skip the transform — a
    # green build with the wrong topology. Reject as repairable instead.
    pipeline = {
        "sources": {
            "source": {
                "plugin": "csv",
                "options": {},
                "on_success": "output",
                "on_validation_failure": "discard",
            }
        },
        "nodes": [
            {
                "id": "output",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "output",
                "on_success": "final_out",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            }
        ],
        "edges": [],
        "outputs": [
            {"sink_name": "final_out", "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }

    with pytest.raises(GuidedCandidateBindingRejected) as raised:
        bind_guided_reviewed_components(pipeline, _guided())

    assert str(raised.value).startswith(_CANDIDATE_SHAPE_INTEGRITY_PREFIX)
    assert raised.value.error_code == "guided_reviewed_name_shadowed"
    assert raised.value.connectivity == {"shadowed_reviewed_names": ["output"]}


def test_bind_rejects_alias_matching_reviewed_name_that_also_names_a_node() -> None:
    # When the candidate alias EQUALS the reviewed name, the alias gate's
    # equality short-circuit used to skip the topology-collision check
    # entirely — the same shadow slipping through the other door.
    pipeline = {
        "sources": {
            "source": {
                "plugin": "csv",
                "options": {},
                "on_success": "output",
                "on_validation_failure": "discard",
            }
        },
        "nodes": [
            {
                "id": "output",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "output",
                "on_success": "output",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            }
        ],
        "edges": [],
        "outputs": [
            {"sink_name": "output", "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }

    with pytest.raises(GuidedCandidateBindingRejected) as raised:
        bind_guided_reviewed_components(pipeline, _guided())

    assert raised.value.error_code == "guided_reviewed_name_shadowed"
    assert raised.value.connectivity == {"shadowed_reviewed_names": ["output"]}


def test_bind_rejects_dangling_reference_in_multi_output_candidate() -> None:
    # Multi-output candidates get the same unknown-target rejection as the
    # single-output case — previously they slid through to validation, whose
    # source-attributed entries project config-masked (blind repair).
    pipeline = _two_output_candidate("main_out", "spill_out")
    pipeline["sources"]["source"]["on_success"] = "stale_rows"  # type: ignore[index]

    with pytest.raises(GuidedCandidateBindingRejected) as raised:
        bind_guided_reviewed_components(pipeline, _guided_two_outputs())

    assert raised.value.error_code == "guided_route_target_unknown"
    assert raised.value.connectivity == {
        "dangling_references": ["stale_rows"],
        "declared_sinks": ["output", "quarantine"],
        "consumable_connections": ["triage"],
    }


def test_bind_leaves_unknown_gate_route_targets_for_validation() -> None:
    # Counterpart boundary to the rename tests: when the rename map is EMPTY
    # (outputs already use the reviewed name) an unknown route target is NOT
    # resolved to the single output the way on_success is. A route value
    # outside the known set is ambiguous — stale sink name vs. a connection
    # whose consumer is not in this candidate (predecessor routes in the
    # amend/correction flows are exactly that) — and a binder rewrite there
    # reads as a planner contract violation in the amend diff (the 1f7241de
    # failure class). Ambiguity belongs to validation.
    pipeline = {
        "sources": {
            "source": {
                "plugin": "csv",
                "options": {},
                "on_success": "hex_gate",
                "on_validation_failure": "discard",
            }
        },
        "nodes": [
            {
                "id": "hex_gate",
                "node_type": "gate",
                "input": "hex_gate",
                "condition": "row['hex'] != ''",
                "routes": {"true": "csv_rows", "false": "discard"},
            }
        ],
        "edges": [],
        "outputs": [
            {"sink_name": "output", "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }

    bound = bind_guided_reviewed_components(pipeline, _guided())

    assert bound["nodes"][0]["routes"] == {"true": "csv_rows", "false": "discard"}


def test_bind_leaves_unknown_fork_to_targets_for_validation() -> None:
    # fork_to twin of the route-target boundary above: an unknown branch name
    # stays for validation rather than being inferred to the reviewed sink.
    # Live branch names (consumed by a transform input) are untouched too.
    pipeline = {
        "sources": {
            "source": {
                "plugin": "csv",
                "options": {},
                "on_success": "fan_out",
                "on_validation_failure": "discard",
            }
        },
        "nodes": [
            {
                "id": "fan_out",
                "node_type": "gate",
                "input": "fan_out",
                "condition": "True",
                "routes": {"true": "fork", "false": "fork"},
                "fork_to": ["csv_rows", "audit_branch"],
            },
            {
                "id": "audit",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "audit_branch",
                "on_success": "output",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
        ],
        "edges": [],
        "outputs": [
            {"sink_name": "output", "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }

    bound = bind_guided_reviewed_components(pipeline, _guided())

    nodes_by_id = {node["id"]: node for node in bound["nodes"]}
    assert nodes_by_id["fan_out"]["fork_to"] == ["csv_rows", "audit_branch"]
    assert nodes_by_id["fan_out"]["routes"] == {"true": "fork", "false": "fork"}


def _fork_coalesce_pipeline() -> dict[str, object]:
    # Mirror of the committed, engine-executed freeform A/B topology (audit
    # run 30496440, 2026-07-22): fan_out gate -> two branch transforms
    # publishing intermediate connections (tone_done / usage_done) -> coalesce
    # keyed on those connections -> shaping transform -> reviewed sink. The
    # intermediate connection names appear ONLY as branch-transform
    # ``on_success`` values and coalesce ``branches`` VALUES — no node's
    # ``input`` consumes them and they are not node ids.
    return {
        "sources": {
            "source": {
                "plugin": "csv",
                "options": {},
                "on_success": "rows",
                "on_validation_failure": "discard",
            }
        },
        "nodes": [
            {
                "id": "fan_out",
                "node_type": "gate",
                "input": "rows",
                "condition": "True",
                "routes": {"true": "fork", "false": "fork"},
                "fork_to": ["branch_a", "branch_b"],
            },
            {
                "id": "assess_tone",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "branch_a",
                "on_success": "tone_done",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
            {
                "id": "assess_usage",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "branch_b",
                "on_success": "usage_done",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
            {
                "id": "merge_branches",
                "node_type": "coalesce",
                "input": "branches",
                "branches": {"branch_a": "tone_done", "branch_b": "usage_done"},
                "policy": "require_all",
                "merge": "union",
                "options": {"schema": {"mode": "observed"}},
            },
            {
                "id": "shape_output",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "merge_branches",
                "on_success": "output",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
        ],
        "edges": [],
        "outputs": [
            {"sink_name": "output", "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }


def test_bind_preserves_coalesce_branch_connection_names() -> None:
    # Guided session 1f7241de (2026-07-22, guided A/B run 15): every candidate
    # — first, both repairs, AND the opus escape hatch — died with exactly
    # ``coalesce_branch_unreachable``. Root cause: the single-output dangling
    # resolution treated the branch transforms' intermediate connections
    # (consumed only as coalesce ``branches`` values) as dangling sink
    # references and rewrote their ``on_success`` to the reviewed sink,
    # manufacturing the rejection — and the sink-lure facts then blamed the
    # planner for the binder's own rewrite.
    bound = bind_guided_reviewed_components(_fork_coalesce_pipeline(), _guided())

    nodes_by_id = {node["id"]: node for node in bound["nodes"]}
    assert nodes_by_id["assess_tone"]["on_success"] == "tone_done"
    assert nodes_by_id["assess_usage"]["on_success"] == "usage_done"
    assert nodes_by_id["merge_branches"]["branches"] == {"branch_a": "tone_done", "branch_b": "usage_done"}


def test_bound_fork_coalesce_candidate_has_reachable_branches() -> None:
    # End-to-end guard: the bound candidate must survive the exact validate()
    # check that emits ``coalesce_branch_unreachable`` — a topology proven
    # legal by three consecutive green engine runs must not be rejected by
    # the guided surface's own binding step.
    from elspeth.web.composer.guided.planning import _canonical_state_from_private_pipeline

    bound = bind_guided_reviewed_components(_fork_coalesce_pipeline(), _guided())
    state = _canonical_state_from_private_pipeline(dict(bound))
    summary = state.validate()

    assert "coalesce_branch_unreachable" not in [entry.error_code for entry in summary.errors]


def test_bind_keeps_discard_routing_untouched() -> None:
    # "discard" is a legal routing destination, not a dangling reference —
    # the single-output inference must never rewrite it (caught by the
    # parity isomorphism matrix: on_error 'discard' became the output name).
    pipeline = {
        "sources": {
            "source": {
                "plugin": "csv",
                "options": {},
                "on_success": "clean",
                "on_validation_failure": "discard",
            }
        },
        "nodes": [
            {
                "id": "clean",
                "node_type": "transform",
                "plugin": "passthrough",
                "options": {},
                "input": "clean",
                "on_success": "output",
                "on_error": "discard",
            }
        ],
        "edges": [],
        "outputs": [
            {"sink_name": "output", "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }

    bound = bind_guided_reviewed_components(pipeline, _guided())

    assert bound["nodes"][0]["on_error"] == "discard"
    assert bound["nodes"][0]["on_success"] == "output"


def _guided_with_output(
    *,
    required_fields: tuple[str, ...],
    output_options: dict[str, object],
) -> GuidedSession:
    from dataclasses import replace

    guided = _guided()
    reviewed = dict(guided.reviewed_outputs)
    reviewed[OUTPUT_ID] = SinkOutputResolved(
        name="output",
        plugin="json",
        options=output_options,
        required_fields=required_fields,
        schema_mode="observed",
        on_write_failure="discard",
    )
    return replace(guided, reviewed_outputs=reviewed)


def _linear_pipeline() -> dict[str, object]:
    return {
        "sources": {
            "source": {
                "plugin": "csv",
                "options": {},
                "on_success": "output",
                "on_validation_failure": "discard",
            }
        },
        "nodes": [],
        "edges": [],
        "outputs": [
            {"sink_name": "output", "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }


def _correction_predecessor() -> CompositionState:
    return CompositionState(
        sources={
            "source": SourceSpec(
                plugin="csv",
                on_success="amount_gate",
                options={"path": "blob:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
                on_validation_failure="discard",
            )
        },
        nodes=(
            NodeSpec(
                id="amount_gate",
                node_type="gate",
                plugin=None,
                input="amount_gate",
                on_success=None,
                on_error=None,
                options={"schema": {"mode": "observed"}},
                condition="row['amount'] > 500",
                routes={"true": "high_value", "false": "standard"},
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
            NodeSpec(
                id="summarize_standard",
                node_type="transform",
                plugin="llm",
                input="high_value",
                on_success="format_high_value_input",
                on_error="discard",
                options={
                    "profile": "task-role",
                    "prompt_template": "Summarize {row[amount]} without changing the amount.",
                    "response_field": "summary",
                    "schema": {"mode": "observed"},
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
            NodeSpec(
                id="format_high_value",
                node_type="transform",
                plugin="field_mapper",
                input="format_high_value_input",
                on_success="output",
                on_error="discard",
                options={
                    "mapping": {"amount": "amount", "tier": "'high'"},
                    "select_only": False,
                    "schema": {"mode": "observed", "required_fields": ["amount"]},
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(
                name="output",
                plugin="json",
                options={"path": "outputs/rows.jsonl"},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def _node_correction_target(owner_key: str, *, stable_id: str) -> GuidedCorrectionTarget:
    return GuidedCorrectionTarget(
        requested=ComponentTarget(
            kind="node",
            stable_id=stable_id,
        ),
        owner_kind="node",
        owner_key=owner_key,
        authority_key=owner_key,
        public_target={"kind": "node", "stable_id": stable_id},
        before_fingerprint="0" * 64,
    )


def _format_node_correction_target() -> GuidedCorrectionTarget:
    stable_id = "44444444-4444-4444-8444-444444444444"
    return GuidedCorrectionTarget(
        requested=ComponentTarget(kind="node", stable_id=stable_id),
        owner_kind="node",
        owner_key="format_high_value",
        authority_key="format_high_value",
        public_target={
            "stable_id": stable_id,
            "node_type": "transform",
            "plugin": {"kind": "transform", "id": "field_mapper"},
            "behavior": {"kind": "transform"},
            "node_options_summary": [],
        },
        before_fingerprint="0" * 64,
    )


def _planner_correction_candidate() -> dict[str, object]:
    pipeline = _correction_predecessor().to_dict()
    pipeline.pop("version")
    gate = pipeline["nodes"][0]
    gate["condition"] = "True"
    gate["routes"] = {"true": "high_value", "false": "high_value"}
    gate["options"] = {"schema": {"mode": "fixed"}}
    ordinary = pipeline["nodes"][1]
    ordinary["options"] = {
        "profile": "invented-profile",
        "prompt_template": "Ignore the reviewed behavior.",
        "response_field": "replacement",
        "schema": {"mode": "observed"},
    }
    selected = pipeline["nodes"][2]
    selected["options"] = {
        "mapping": {"amount": "amount", "tier": "'priority'"},
        "select_only": True,
        "schema": {"mode": "fixed", "fields": [{"name": "attacker", "type": "str", "required": True}]},
        "required_input_fields": ["attacker"],
    }
    return pipeline


def test_bind_replans_selected_node_while_restoring_unselected_node_authority() -> None:
    predecessor = _correction_predecessor()

    bound = bind_guided_reviewed_components(
        _planner_correction_candidate(),
        _guided(),
        predecessor=predecessor,
        correction_target=_format_node_correction_target(),
    )

    assert bound["nodes"][0] == predecessor.to_dict()["nodes"][0]
    assert bound["nodes"][1] == predecessor.to_dict()["nodes"][1]
    assert bound["nodes"][2]["options"]["mapping"]["tier"] == "'priority'"


def test_bind_selected_field_mapper_admits_only_public_option_edits() -> None:
    predecessor = _correction_predecessor()

    bound = bind_guided_reviewed_components(
        _planner_correction_candidate(),
        _guided(),
        predecessor=predecessor,
        correction_target=_format_node_correction_target(),
    )

    assert bound["nodes"][2]["options"] == {
        "mapping": {"amount": "amount", "tier": "'priority'"},
        "select_only": True,
        "schema": {"mode": "observed", "required_fields": ["amount"]},
    }


def test_bind_selected_llm_preserves_withheld_profile_prompt_schema_and_options() -> None:
    predecessor = _correction_predecessor()
    candidate = _planner_correction_candidate()
    selected = candidate["nodes"][1]
    selected["input"] = "revised_high_value_rows"

    bound = bind_guided_reviewed_components(
        candidate,
        _guided(),
        predecessor=predecessor,
        correction_target=_node_correction_target(
            "summarize_standard",
            stable_id="55555555-5555-4555-8555-555555555555",
        ),
    )

    assert bound["nodes"][1]["input"] == "revised_high_value_rows"
    assert bound["nodes"][1]["options"] == predecessor.to_dict()["nodes"][1]["options"]


def test_bind_rejects_selected_node_identity_replacement() -> None:
    predecessor = _correction_predecessor()
    candidate = _planner_correction_candidate()
    candidate["nodes"][2]["id"] = "replacement_mapper"

    with pytest.raises(AuditIntegrityError, match="selected predecessor node identity"):
        bind_guided_reviewed_components(
            candidate,
            _guided(),
            predecessor=predecessor,
            correction_target=_format_node_correction_target(),
        )


@pytest.mark.parametrize("malformed_options", ["not-a-dict", None, ["mapping"], 7], ids=["str", "missing", "list", "int"])
def test_bind_rejects_malformed_selected_node_options_as_repairable_candidate_shape(malformed_options: object) -> None:
    """Regression for elspeth-d923304d18 residue: a selected node whose
    options are not a dict is a provider authoring slip. The raise must carry
    the ``guided planner candidate`` prefix so the planner loop answers it
    with one budgeted repair turn instead of terminalizing the guided
    operation.
    """
    predecessor = _correction_predecessor()
    candidate = _planner_correction_candidate()
    if malformed_options is None:
        del candidate["nodes"][2]["options"]
    else:
        candidate["nodes"][2]["options"] = malformed_options

    with pytest.raises(GuidedCandidateBindingRejected) as raised:
        bind_guided_reviewed_components(
            candidate,
            _guided(),
            predecessor=predecessor,
            correction_target=_format_node_correction_target(),
        )
    assert str(raised.value).startswith(_CANDIDATE_SHAPE_INTEGRITY_PREFIX)
    assert raised.value.error_code == "guided_delta_authority_violation"


def test_bind_rejects_selected_gate_replaced_with_passthrough_transform() -> None:
    predecessor = _correction_predecessor()
    candidate = _planner_correction_candidate()
    selected = candidate["nodes"][0]
    selected.update(
        {
            "node_type": "transform",
            "plugin": "passthrough",
            "on_success": "high_value",
            "on_error": "discard",
            "condition": None,
            "routes": None,
            "options": {},
        }
    )

    with pytest.raises(AuditIntegrityError, match="selected predecessor node type or plugin"):
        bind_guided_reviewed_components(
            candidate,
            _guided(),
            predecessor=predecessor,
            correction_target=_node_correction_target(
                "amount_gate",
                stable_id="66666666-6666-4666-8666-666666666666",
            ),
        )


def test_bind_rejects_selected_transform_plugin_substitution() -> None:
    predecessor = _correction_predecessor()
    candidate = _planner_correction_candidate()
    selected = candidate["nodes"][1]
    selected["plugin"] = "passthrough"
    selected["options"] = {}

    with pytest.raises(AuditIntegrityError, match="selected predecessor node type or plugin"):
        bind_guided_reviewed_components(
            candidate,
            _guided(),
            predecessor=predecessor,
            correction_target=_node_correction_target(
                "summarize_standard",
                stable_id="55555555-5555-4555-8555-555555555555",
            ),
        )


def test_bind_does_not_accept_planner_override_of_unselected_withheld_fields() -> None:
    predecessor = _correction_predecessor()
    candidate = _planner_correction_candidate()
    candidate["nodes"][2] = predecessor.to_dict()["nodes"][2]

    bound = bind_guided_reviewed_components(
        candidate,
        _guided(),
        predecessor=predecessor,
        correction_target=_format_node_correction_target(),
    )

    rebound_gate = bound["nodes"][0]
    assert rebound_gate["condition"] == "row['amount'] > 500"
    assert rebound_gate["routes"] == {"true": "high_value", "false": "standard"}
    assert rebound_gate["options"] == {"schema": {"mode": "observed"}}
    assert bound["nodes"][1]["options"]["prompt_template"] == "Summarize {row[amount]} without changing the amount."


def test_bind_reapplies_node_custody_after_topology_repairs() -> None:
    predecessor = _correction_predecessor()
    candidate = _planner_correction_candidate()
    candidate["nodes"][2]["input"] = "replacement_input"

    bound = bind_guided_reviewed_components(
        candidate,
        _guided(),
        predecessor=predecessor,
        correction_target=_format_node_correction_target(),
    )

    assert bound["nodes"][1]["on_success"] == "format_high_value_input"
    assert bound["nodes"][2]["input"] == "replacement_input"


def test_bind_correction_rejects_stale_invented_sink_alias_in_routing() -> None:
    # The correction flow lost the old single-output dangling repair when
    # elspeth-572c642dbf replaced it with the plain-flow rejection and
    # excluded correction candidates entirely. A stale invented sink alias
    # then fell through to graph validation, whose source-attributed entries
    # project config-masked — no connectivity facts — so the repair loop
    # burned to REPAIR_EXHAUSTED on a defect the plain flow repairs in one
    # turn. A name appearing in NEITHER the bound topology NOR the
    # predecessor is provably not withheld structure: reject it with the
    # same one-turn repair facts as the plain flow.
    candidate = _planner_correction_candidate()
    candidate["nodes"][2]["on_success"] = "results_json"

    with pytest.raises(GuidedCandidateBindingRejected) as raised:
        bind_guided_reviewed_components(
            candidate,
            _guided(),
            predecessor=_correction_predecessor(),
            correction_target=_format_node_correction_target(),
        )

    assert raised.value.error_code == "guided_route_target_unknown"
    assert raised.value.connectivity == {
        "dangling_references": ["results_json"],
        "declared_sinks": ["output"],
        "consumable_connections": ["amount_gate", "format_high_value_input", "high_value"],
    }


def test_bind_correction_admits_predecessor_mentioned_names_for_validation() -> None:
    # Amnesty boundary: "standard" is mentioned only by the predecessor
    # gate's withheld routes{} — no candidate node consumes it — yet it is
    # exactly the withheld-structure placeholder the correction flow must
    # tolerate (the selected node's rewiring is adjudicated downstream).
    # Every name the predecessor mentions anywhere stays for validation
    # rather than being rejected as a stale alias.
    candidate = _planner_correction_candidate()
    candidate["nodes"][2]["on_success"] = "standard"

    bound = bind_guided_reviewed_components(
        candidate,
        _guided(),
        predecessor=_correction_predecessor(),
        correction_target=_format_node_correction_target(),
    )

    assert bound["nodes"][2]["on_success"] == "standard"


def _coalesce_correction_predecessor() -> CompositionState:
    # fork -> two branch transforms -> coalesce -> shaper -> sink. The
    # coalesce ``branches`` VALUES (tone_done / usage_done) are withheld
    # structural behavior: guided_redacted_current_state_context projects
    # id/input/on_success/on_error only, never routes/fork_to/branches.
    return CompositionState(
        sources={
            "source": SourceSpec(
                plugin="csv",
                on_success="rows",
                options={"path": "blob:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
                on_validation_failure="discard",
            )
        },
        nodes=(
            NodeSpec(
                id="fan_out",
                node_type="gate",
                plugin=None,
                input="rows",
                on_success=None,
                on_error=None,
                options={"schema": {"mode": "observed"}},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("branch_a", "branch_b"),
                branches=None,
                policy=None,
                merge=None,
            ),
            NodeSpec(
                id="assess_tone",
                node_type="transform",
                plugin="passthrough",
                input="branch_a",
                on_success="tone_done",
                on_error="discard",
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
            NodeSpec(
                id="assess_usage",
                node_type="transform",
                plugin="passthrough",
                input="branch_b",
                on_success="usage_done",
                on_error="discard",
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
            NodeSpec(
                id="merge_branches",
                node_type="coalesce",
                plugin=None,
                input="branches",
                on_success=None,
                on_error=None,
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches={"branch_a": "tone_done", "branch_b": "usage_done"},
                policy="require_all",
                merge="union",
            ),
            NodeSpec(
                id="shape_output",
                node_type="transform",
                plugin="passthrough",
                input="merge_branches",
                on_success="output",
                on_error="discard",
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(
                name="output",
                plugin="json",
                options={"path": "outputs/rows.jsonl"},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def test_bind_correction_rejection_facts_stay_candidate_authored() -> None:
    # Custody guard on the new correction-flow rejection: the amnesty set may
    # consult withheld predecessor structure (branches VALUES here) to admit
    # targets server-side, but the connectivity facts crossing back to the
    # provider must be built from the model's own candidate — a restored
    # coalesce's branch connections must never surface in
    # ``consumable_connections``.
    predecessor = _coalesce_correction_predecessor()
    candidate = {
        "sources": {
            "source": {
                "plugin": "csv",
                "options": {},
                "on_success": "rows",
                "on_validation_failure": "discard",
            }
        },
        # The redacted reconstruction the provider can honestly author: no
        # routes/fork_to/branches keys — those fields are withheld.
        "nodes": [
            {"id": "fan_out", "node_type": "gate", "plugin": None, "input": "rows", "on_success": None, "on_error": None},
            {
                "id": "assess_tone",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "branch_a",
                "on_success": "results_json",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
            {
                "id": "assess_usage",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "branch_b",
                "on_success": "usage_done",
                "on_error": "discard",
            },
            {"id": "merge_branches", "node_type": "coalesce", "plugin": None, "input": "branches", "on_success": None, "on_error": None},
            {
                "id": "shape_output",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "merge_branches",
                "on_success": "output",
                "on_error": "discard",
            },
        ],
        "edges": [],
        "outputs": [
            {"sink_name": "output", "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }

    with pytest.raises(GuidedCandidateBindingRejected) as raised:
        bind_guided_reviewed_components(
            candidate,
            _guided(),
            predecessor=predecessor,
            correction_target=_node_correction_target(
                "assess_tone",
                stable_id="66666666-6666-4666-8666-666666666666",
            ),
        )

    assert raised.value.error_code == "guided_route_target_unknown"
    assert raised.value.connectivity["dangling_references"] == ["results_json"]
    consumable = raised.value.connectivity["consumable_connections"]
    assert "tone_done" not in consumable
    assert "usage_done" not in consumable


def _amend_authority() -> GuidedRevisionAuthority:
    return GuidedRevisionAuthority(mode="amend", predecessor=_correction_predecessor())


def _minimal_amend_reconstruction() -> dict[str, object]:
    """What the redacted provider can honestly reconstruct for old nodes."""
    predecessor = _correction_predecessor().to_dict()
    predecessor.pop("version")
    predecessor["nodes"] = [
        {
            "id": node["id"],
            "node_type": node["node_type"],
            "plugin": node["plugin"],
            "input": node["input"],
            "on_success": node["on_success"],
        }
        for node in predecessor["nodes"]
    ]
    return predecessor


def test_bind_prose_amend_restores_every_withheld_predecessor_node_field() -> None:
    predecessor = _correction_predecessor()

    result = guided_planning.bind_guided_prose_revision_candidate(
        _minimal_amend_reconstruction(),
        _guided(),
        authority=_amend_authority(),
    )

    assert result.rejection_code is None
    assert result.pipeline["nodes"] == predecessor.to_dict()["nodes"]
    assert result.pipeline["nodes"][0]["condition"] == "row['amount'] > 500"
    assert result.pipeline["nodes"][0]["routes"] == {"true": "high_value", "false": "standard"}
    assert result.pipeline["nodes"][1]["options"] == predecessor.to_dict()["nodes"][1]["options"]


def test_bind_prose_amend_allows_only_insertion_rewiring_of_existing_nodes() -> None:
    candidate = _correction_predecessor().to_dict()
    candidate.pop("version")
    candidate["sources"]["source"]["on_success"] = "normalize_input"
    candidate["nodes"].insert(
        0,
        {
            "id": "normalize_amount",
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "normalize_input",
            "on_success": "amount_gate",
            "on_error": "discard",
            "options": {"schema": {"mode": "observed"}},
        },
    )

    result = guided_planning.bind_guided_prose_revision_candidate(
        candidate,
        _guided(),
        authority=_amend_authority(),
    )

    assert result.rejection_code is None
    assert result.pipeline["sources"]["source"]["on_success"] == "normalize_input"
    assert result.pipeline["nodes"][0]["id"] == "normalize_amount"
    assert result.pipeline["nodes"][1:] == _correction_predecessor().to_dict()["nodes"]


@pytest.mark.parametrize("laundered_target", ("source", "output", "amount_gate"))
def test_bind_prose_amend_rejects_new_node_laundering_of_existing_targets(
    laundered_target: str,
) -> None:
    candidate = _correction_predecessor().to_dict()
    candidate.pop("version")
    candidate["nodes"].append(
        {
            "id": "new_transform",
            "node_type": "transform",
            "plugin": "passthrough",
            "input": laundered_target,
            "on_success": "new_rows",
            "on_error": "discard",
            "options": {"schema": {"mode": "observed"}},
        }
    )
    candidate["nodes"][1]["on_success"] = laundered_target

    result = guided_planning.bind_guided_prose_revision_candidate(
        candidate,
        _guided(),
        authority=_amend_authority(),
    )

    assert result.rejection_code == "guided_amend_contract_violation"
    assert result.pipeline["nodes"][:3] == _correction_predecessor().to_dict()["nodes"]


def test_bind_prose_amend_rejects_existing_node_relative_order_changes() -> None:
    candidate = _correction_predecessor().to_dict()
    candidate.pop("version")
    candidate["nodes"][0], candidate["nodes"][1] = candidate["nodes"][1], candidate["nodes"][0]

    result = guided_planning.bind_guided_prose_revision_candidate(
        candidate,
        _guided(),
        authority=_amend_authority(),
    )

    assert result.rejection_code == "guided_amend_contract_violation"
    assert result.pipeline["nodes"] == _correction_predecessor().to_dict()["nodes"]


def test_prose_revision_successor_recheck_rejects_omitted_private_fields_with_a_real_insertion() -> None:
    candidate = _correction_predecessor().to_dict()
    candidate["sources"]["source"]["on_success"] = "normalize_input"
    candidate["nodes"][1].pop("options")
    candidate["nodes"].insert(
        0,
        {
            "id": "normalize_amount",
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "normalize_input",
            "on_success": "amount_gate",
            "on_error": "discard",
            "options": {"schema": {"mode": "observed"}},
            "condition": None,
            "routes": None,
            "fork_to": None,
            "branches": None,
            "policy": None,
            "merge": None,
        },
    )
    successor = guided_planning._canonical_state_from_private_pipeline(candidate)

    with pytest.raises(AuditIntegrityError, match="violates amend authority"):
        guided_planning.require_guided_prose_revision_successor(
            successor,
            _guided(),
            authority=_amend_authority(),
        )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda nodes: nodes.pop(0),
        lambda nodes: nodes.append(dict(nodes[0])),
        lambda nodes: nodes[0].__setitem__("node_type", "transform"),
        lambda nodes: nodes[1].__setitem__("plugin", "passthrough"),
        lambda nodes: nodes[0].__setitem__("condition", "True"),
        lambda nodes: nodes[0].__setitem__("routes", {"true": "high_value", "false": "high_value"}),
        lambda nodes: nodes[1].__setitem__("options", {"prompt_template": "replace reviewed behavior"}),
        lambda nodes: nodes[1].__setitem__("on_error", "output"),
        lambda nodes: nodes[1].__setitem__("new_control", {"route": "output"}),
    ),
    ids=(
        "removed",
        "duplicate",
        "node-type",
        "plugin",
        "gate-condition",
        "gate-routes",
        "private-options",
        "control-route",
        "new-control-key",
    ),
)
def test_bind_prose_amend_marks_contract_violations_for_bounded_repair(mutate: object) -> None:
    candidate = _correction_predecessor().to_dict()
    candidate.pop("version")
    mutate(candidate["nodes"])

    result = guided_planning.bind_guided_prose_revision_candidate(
        candidate,
        _guided(),
        authority=_amend_authority(),
    )

    assert result.rejection_code == "guided_amend_contract_violation"
    assert result.pipeline["nodes"] == _correction_predecessor().to_dict()["nodes"]


def test_bind_explicit_prose_replace_permits_node_removal() -> None:
    predecessor = _correction_predecessor()
    candidate = predecessor.to_dict()
    candidate.pop("version")
    candidate["nodes"] = []
    candidate["sources"]["source"]["on_success"] = "output"

    result = guided_planning.bind_guided_prose_revision_candidate(
        candidate,
        _guided(),
        authority=GuidedRevisionAuthority(mode="replace", predecessor=predecessor),
    )

    assert result.rejection_code is None
    assert result.pipeline["nodes"] == []
    assert result.pipeline["sources"]["source"]["on_success"] == "output"


def test_bind_explicit_prose_replace_rejects_stale_invented_sink_alias() -> None:
    # Replace has explicit destructive topology authority and — unlike amend —
    # NO adjudication of its own after binding, so opting out of the
    # route-target rejection left a stale invented sink alias to die at
    # config-masked validation (blind repair, REPAIR_EXHAUSTED). Replace gets
    # plain-flow enforcement: the model owns every route it wrote.
    predecessor = _correction_predecessor()
    candidate = predecessor.to_dict()
    candidate.pop("version")
    candidate["nodes"] = []
    candidate["sources"]["source"]["on_success"] = "results_json"

    with pytest.raises(GuidedCandidateBindingRejected) as raised:
        guided_planning.bind_guided_prose_revision_candidate(
            candidate,
            _guided(),
            authority=GuidedRevisionAuthority(mode="replace", predecessor=predecessor),
        )

    assert raised.value.error_code == "guided_route_target_unknown"
    assert raised.value.connectivity == {
        "dangling_references": ["results_json"],
        "declared_sinks": ["output"],
        "consumable_connections": [],
    }


def test_bind_explicit_prose_replace_admits_predecessor_mentioned_names_for_validation() -> None:
    # Amnesty parity with the correction flow: the replace provider saw only
    # the REDACTED predecessor (routes/fork_to/branches withheld), so the
    # server cannot distinguish a stale reference to a removed consumer from
    # an honest re-emission of withheld structure. Every predecessor-mentioned
    # name is admitted server-side and left to validation; only a name in
    # NEITHER the candidate NOR the predecessor stays rejected as provably
    # invented (previous test).
    predecessor = _correction_predecessor()
    candidate = predecessor.to_dict()
    candidate.pop("version")
    candidate["nodes"] = []
    candidate["sources"]["source"]["on_success"] = "amount_gate"

    result = guided_planning.bind_guided_prose_revision_candidate(
        candidate,
        _guided(),
        authority=GuidedRevisionAuthority(mode="replace", predecessor=predecessor),
    )

    assert result.rejection_code is None
    assert result.pipeline["sources"]["source"]["on_success"] == "amount_gate"


def test_bind_explicit_prose_replace_admits_withheld_branch_connections() -> None:
    # A fork→coalesce predecessor's branch connections (tone_done/usage_done)
    # exist ONLY in the withheld coalesce ``branches`` mapping — the redacted
    # provider context projects id/input/on_success/on_error and never
    # routes/fork_to/branches. A model faithfully re-emitting exactly what it
    # was shown therefore names branch connections whose consumer it cannot
    # know. Without predecessor amnesty the binder rejected every such
    # candidate (guided_route_target_unknown on tone_done/usage_done) and the
    # repair prompt steered the model into re-pointing honest branch wiring
    # at sinks — the 1f7241de failure class, burning to REPAIR_EXHAUSTED.
    predecessor = _coalesce_correction_predecessor()
    candidate = {
        "sources": {
            "source": {
                "plugin": "csv",
                "options": {},
                "on_success": "rows",
                "on_validation_failure": "discard",
            }
        },
        # The redacted reconstruction the provider can honestly author: no
        # routes/fork_to/branches keys — those fields were withheld.
        "nodes": [
            {"id": "fan_out", "node_type": "gate", "plugin": None, "input": "rows", "on_success": None, "on_error": None},
            {
                "id": "assess_tone",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "branch_a",
                "on_success": "tone_done",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
            {
                "id": "assess_usage",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "branch_b",
                "on_success": "usage_done",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
            {"id": "merge_branches", "node_type": "coalesce", "plugin": None, "input": "branches", "on_success": None, "on_error": None},
            {
                "id": "shape_output",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "merge_branches",
                "on_success": "output",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
        ],
        "edges": [],
        "outputs": [
            {"sink_name": "output", "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }

    result = guided_planning.bind_guided_prose_revision_candidate(
        candidate,
        _guided(),
        authority=GuidedRevisionAuthority(mode="replace", predecessor=predecessor),
    )

    assert result.rejection_code is None
    assert result.pipeline["nodes"][1]["on_success"] == "tone_done"
    assert result.pipeline["nodes"][2]["on_success"] == "usage_done"


class TestBindDeclaredRequiredFields:
    """F3 fix 3a: reviewed declared output fields become the sink's schema contract.

    Step-2 field review captures ``SinkOutputResolved.required_fields``, but
    both the composer sink-contract check and the runtime DAG validation key
    off ``options.schema.required_fields`` — the binder is the one seam that
    reaches candidate validation, the sealed proposal, committed state, YAML,
    and runtime.
    """

    def test_declared_fields_materialize_as_the_sanctioned_schema_expression(self) -> None:
        # No author schema block at all: the binder writes the sanctioned
        # observed-mode contract expression (contracts/schema.py docs).
        guided = _guided_with_output(
            required_fields=("name", "score"),
            output_options={"path": "outputs/colours.json"},
        )

        bound = bind_guided_reviewed_components(_linear_pipeline(), guided)

        assert bound["outputs"][0]["options"] == {
            "path": "outputs/colours.json",
            "schema": {"mode": "observed", "required_fields": ["name", "score"]},
        }

    def test_empty_declared_fields_leave_reviewed_options_byte_identical(self) -> None:
        options = {"path": "outputs/colours.json", "schema": {"mode": "observed"}}
        guided = _guided_with_output(required_fields=(), output_options=dict(options))

        bound = bind_guided_reviewed_components(_linear_pipeline(), guided)

        assert bound["outputs"][0]["options"] == options
        assert "required_fields" not in bound["outputs"][0]["options"]["schema"]

    def test_author_typed_required_fields_merge_as_a_union_with_author_order_first(self) -> None:
        # Author-typed schema.required_fields is never overwritten: the union
        # keeps the author's entries and order, appending only the declared
        # fields not already present.
        guided = _guided_with_output(
            required_fields=("name", "score"),
            output_options={
                "path": "outputs/colours.json",
                "schema": {"mode": "observed", "required_fields": ["email", "name"]},
            },
        )

        bound = bind_guided_reviewed_components(_linear_pipeline(), guided)

        schema = bound["outputs"][0]["options"]["schema"]
        assert schema == {"mode": "observed", "required_fields": ["email", "name", "score"]}

    def test_declared_fields_merge_under_the_schema_config_alias_without_minting_schema(self) -> None:
        guided = _guided_with_output(
            required_fields=("name",),
            output_options={"path": "outputs/colours.json", "schema_config": {"mode": "observed"}},
        )

        bound = bind_guided_reviewed_components(_linear_pipeline(), guided)

        options = bound["outputs"][0]["options"]
        assert "schema" not in options
        assert options["schema_config"] == {"mode": "observed", "required_fields": ["name"]}

    def test_author_schema_keys_beyond_required_fields_are_preserved(self) -> None:
        guided = _guided_with_output(
            required_fields=("email",),
            output_options={
                "path": "outputs/colours.json",
                "schema": {"mode": "fixed", "fields": ["email: str"], "guaranteed_fields": ["email"]},
            },
        )

        bound = bind_guided_reviewed_components(_linear_pipeline(), guided)

        assert bound["outputs"][0]["options"]["schema"] == {
            "mode": "fixed",
            "fields": ["email: str"],
            "guaranteed_fields": ["email"],
            "required_fields": ["email"],
        }


def _source_correction_target() -> GuidedCorrectionTarget:
    return GuidedCorrectionTarget(
        requested=ComponentTarget(kind="source", stable_id=SOURCE_ID),
        owner_kind="source",
        owner_key="source",
        authority_key="source",
        public_target={"kind": "source", "stable_id": SOURCE_ID},
        before_fingerprint="1" * 64,
    )


def _output_correction_target() -> GuidedCorrectionTarget:
    return GuidedCorrectionTarget(
        requested=ComponentTarget(kind="output", stable_id=OUTPUT_ID),
        owner_kind="output",
        owner_key="output",
        authority_key="output",
        public_target={"kind": "output", "stable_id": OUTPUT_ID},
        before_fingerprint="2" * 64,
    )


def test_initial_guided_delta_materializes_fork_coalesce_and_multi_output_topologies() -> None:
    fork = _fork_coalesce_pipeline()
    fork_bound = materialize_guided_authorized_candidate(
        {
            "source_routes": [{"stable_id": SOURCE_ID, "on_success": fork["sources"]["source"]["on_success"]}],
            "nodes": fork["nodes"],
            "edges": fork["edges"],
            "output_targets": [{"stable_id": OUTPUT_ID}],
        },
        authority=None,
        guided=_guided(),
        current_state=_correction_predecessor(),
    )
    assert [node["node_type"] for node in fork_bound["nodes"]] == [
        "gate",
        "transform",
        "transform",
        "coalesce",
        "transform",
    ]
    assert fork_bound["nodes"][3]["branches"] == {"branch_a": "tone_done", "branch_b": "usage_done"}

    multi = _two_output_candidate("output", "quarantine")
    multi_bound = materialize_guided_authorized_candidate(
        {
            "source_routes": [{"stable_id": SOURCE_ID, "on_success": multi["sources"]["source"]["on_success"]}],
            "nodes": multi["nodes"],
            "edges": multi["edges"],
            "output_targets": [{"stable_id": OUTPUT_ID}, {"stable_id": OUTPUT_B_ID}],
        },
        authority=None,
        guided=_guided_two_outputs(),
        current_state=_correction_predecessor(),
    )
    assert [output["sink_name"] for output in multi_bound["outputs"]] == ["output", "quarantine"]
    assert multi_bound["nodes"][0]["on_success"] == "output"
    assert multi_bound["nodes"][0]["on_error"] == "quarantine"


def test_source_correction_changes_only_selected_route_and_may_add_topology() -> None:
    predecessor = _correction_predecessor()
    before = predecessor.to_dict()
    target = _source_correction_target()
    bound = materialize_guided_authorized_candidate(
        {
            "source_routes": [{"stable_id": SOURCE_ID, "on_success": "screen_input"}],
            "nodes": [
                {
                    "id": "screen_rows",
                    "node_type": "transform",
                    "plugin": "passthrough",
                    "input": "screen_input",
                    "on_success": "amount_gate",
                    "on_error": "discard",
                    "options": {"schema": {"mode": "observed"}},
                }
            ],
            "edges": [
                {
                    "id": "source_to_screen",
                    "from_node": "source",
                    "to_node": "screen_rows",
                    "edge_type": "on_success",
                }
            ],
        },
        authority=target,
        guided=_guided(),
        current_state=predecessor,
    )

    assert bound["sources"]["source"]["on_success"] == "screen_input"
    assert bound["sources"]["source"]["plugin"] == "csv"
    assert bound["sources"]["source"]["options"] == {"path": "blob:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
    assert bound["nodes"][:-1] == before["nodes"]
    assert bound["nodes"][-1]["id"] == "screen_rows"


def test_node_correction_materializer_preserves_every_unselected_node_byte() -> None:
    predecessor = _correction_predecessor()
    candidate = _planner_correction_candidate()
    selected = candidate["nodes"][2]
    bound = materialize_guided_authorized_candidate(
        {
            "node_patch": {
                "stable_id": _format_node_correction_target().requested.stable_id,
                "options": {
                    "mapping": selected["options"]["mapping"],
                    "select_only": selected["options"]["select_only"],
                },
            },
            "edges": [],
        },
        authority=_format_node_correction_target(),
        guided=_guided(),
        current_state=predecessor,
    )

    before_nodes = predecessor.to_dict()["nodes"]
    assert bound["nodes"][0] == before_nodes[0]
    assert bound["nodes"][1] == before_nodes[1]
    assert bound["nodes"][2]["options"] == {
        "mapping": {"amount": "amount", "tier": "'priority'"},
        "select_only": True,
        "schema": {"mode": "observed", "required_fields": ["amount"]},
    }
    assert bound["outputs"][0]["options"] == {"path": "outputs/colours.json"}


def _public_node_correction_target(
    owner_key: str,
    *,
    stable_id: str,
    node_type: str,
    plugin: str | None,
    behavior: dict[str, object],
) -> GuidedCorrectionTarget:
    return GuidedCorrectionTarget(
        requested=ComponentTarget(kind="node", stable_id=stable_id),
        owner_kind="node",
        owner_key=owner_key,
        authority_key=owner_key,
        public_target={
            "stable_id": stable_id,
            "node_type": node_type,
            "plugin": ({"kind": "transform", "id": plugin} if plugin is not None else None),
            "behavior": behavior,
            "node_options_summary": [],
        },
        before_fingerprint="4" * 64,
    )


def test_node_patch_overlays_gate_condition_without_reauthoring_hidden_routes() -> None:
    predecessor = _correction_predecessor()
    stable_id = "66666666-6666-4666-8666-666666666666"
    target = _public_node_correction_target(
        "amount_gate",
        stable_id=stable_id,
        node_type="gate",
        plugin=None,
        behavior={
            "kind": "gate",
            "condition": "row['amount'] > 500",
            "route_aliases": ["route-1", "route-2"],
            "routes": [{"alias": "route-1", "key": "false"}, {"alias": "route-2", "key": "true"}],
            "fork_branches": [],
        },
    )

    bound = materialize_guided_authorized_candidate(
        {"node_patch": {"stable_id": stable_id, "condition": "row['amount'] >= 500"}, "edges": []},
        authority=target,
        guided=_guided(),
        current_state=predecessor,
    )

    before = predecessor.to_dict()["nodes"][0]
    assert bound["nodes"][0]["condition"] == "row['amount'] >= 500"
    assert bound["nodes"][0]["routes"] == before["routes"]
    assert bound["nodes"][0].get("fork_to") == before.get("fork_to")
    assert bound["nodes"][0]["options"] == before["options"]


def test_node_patch_overlays_coalesce_policy_without_reauthoring_hidden_branches() -> None:
    predecessor = _coalesce_correction_predecessor()
    stable_id = "77777777-7777-4777-8777-777777777777"
    target = _public_node_correction_target(
        "merge_branches",
        stable_id=stable_id,
        node_type="coalesce",
        plugin=None,
        behavior={
            "kind": "coalesce",
            "branch_aliases": ["branch-1", "branch-2"],
            "policy": "require_all",
            "merge": "union",
            "timeout_seconds": None,
        },
    )

    bound = materialize_guided_authorized_candidate(
        {"node_patch": {"stable_id": stable_id, "policy": "best_effort"}, "edges": []},
        authority=target,
        guided=_guided(),
        current_state=predecessor,
    )

    before = predecessor.to_dict()["nodes"][3]
    assert bound["nodes"][3]["policy"] == "best_effort"
    assert bound["nodes"][3]["branches"] == before["branches"]
    assert bound["nodes"][3]["merge"] == before["merge"]
    assert bound["nodes"][3]["options"] == before["options"]


def test_node_patch_overlays_row_union_timeout_without_reauthoring_hidden_branches() -> None:
    coalesce = _coalesce_correction_predecessor()
    row_union = replace(
        coalesce.nodes[3],
        node_type="row_union",
        policy=None,
        merge=None,
        timeout_seconds=15.0,
    )
    predecessor = replace(coalesce, nodes=(*coalesce.nodes[:3], row_union, *coalesce.nodes[4:]))
    stable_id = "88888888-8888-4888-8888-888888888888"
    target = _public_node_correction_target(
        "merge_branches",
        stable_id=stable_id,
        node_type="row_union",
        plugin=None,
        behavior={
            "kind": "row_union",
            "branch_aliases": ["branch-1", "branch-2"],
            "policy": "require_all",
            "timeout_seconds": 15.0,
        },
    )

    bound = materialize_guided_authorized_candidate(
        {"node_patch": {"stable_id": stable_id, "timeout_seconds": 30.0}, "edges": []},
        authority=target,
        guided=_guided(),
        current_state=predecessor,
    )

    before = predecessor.to_dict()["nodes"][3]
    assert bound["nodes"][3]["timeout_seconds"] == 30.0
    assert bound["nodes"][3]["branches"] == before["branches"]
    assert bound["nodes"][3]["options"] == before["options"]


def test_output_correction_reconnects_upstream_scalar_and_preserves_sink_authority() -> None:
    predecessor = _correction_predecessor()
    target = _output_correction_target()
    bound = materialize_guided_authorized_candidate(
        {
            "output_targets": [{"stable_id": OUTPUT_ID}],
            "edges": [
                {
                    "id": "format_to_output",
                    "from_node": "format_high_value",
                    "to_node": "output",
                    "edge_type": "on_success",
                }
            ],
        },
        authority=target,
        guided=_guided(),
        current_state=predecessor,
    )

    assert bound["nodes"][2]["on_success"] == "output"
    assert bound["outputs"] == [
        {
            "sink_name": "output",
            "plugin": "json",
            "options": {"path": "outputs/colours.json"},
            "on_write_failure": "discard",
        }
    ]


def test_correction_delta_rejects_unselected_identity_and_nonincident_routing() -> None:
    predecessor = _correction_predecessor()
    target = _source_correction_target()
    with pytest.raises(GuidedCandidateBindingRejected) as duplicate:
        materialize_guided_authorized_candidate(
            {
                "source_routes": [{"stable_id": SOURCE_ID, "on_success": "rows"}],
                "nodes": [predecessor.to_dict()["nodes"][0]],
                "edges": [],
            },
            authority=target,
            guided=_guided(),
            current_state=predecessor,
        )
    assert duplicate.value.error_code == "guided_delta_duplicate_stable_id"

    with pytest.raises(GuidedCandidateBindingRejected) as nonincident:
        materialize_guided_authorized_candidate(
            {
                "source_routes": [{"stable_id": SOURCE_ID, "on_success": "rows"}],
                "nodes": [],
                "edges": [
                    {
                        "id": "unrelated",
                        "from_node": "amount_gate",
                        "to_node": "output",
                        "edge_type": "on_success",
                    }
                ],
            },
            authority=target,
            guided=_guided(),
            current_state=predecessor,
        )
    assert nonincident.value.error_code == "guided_delta_nonincident_route"


def _candidate_context() -> ToolContext:
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return ToolContext(
        catalog=PolicyCatalogView.for_trained_operator(catalog, snapshot),
        plugin_snapshot=snapshot,
    )


def _boundary_valid_guided(*, multiple_outputs: bool = False) -> GuidedSession:
    guided = _guided_two_outputs() if multiple_outputs else _guided()
    sources = {
        stable_id: replace(
            guided.reviewed_sources[stable_id],
            options={"path": f"{guided.reviewed_sources[stable_id].name}.csv", "schema": {"mode": "observed"}},
        )
        for stable_id in guided.source_order
    }
    outputs = {
        stable_id: replace(
            guided.reviewed_outputs[stable_id],
            options={
                "path": f"outputs/{guided.reviewed_outputs[stable_id].name}.jsonl",
                "schema": {"mode": "observed"},
            },
        )
        for stable_id in guided.output_order
    }
    return replace(guided, reviewed_sources=sources, reviewed_outputs=outputs)


def _boundary_valid_correction_guided() -> GuidedSession:
    guided = _boundary_valid_guided()
    source = guided.reviewed_sources[SOURCE_ID]
    return replace(
        guided,
        reviewed_sources={
            SOURCE_ID: replace(
                source,
                options={"path": "source.csv", "schema": {"mode": "fixed", "fields": ["amount: int"]}},
            )
        },
    )


def _empty_candidate_state() -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _authorized_initial_full_candidate(
    topology: dict[str, object],
    guided: GuidedSession,
) -> dict[str, object]:
    topology_sources = topology["sources"]
    assert type(topology_sources) is dict
    return {
        "sources": {
            reviewed.name: {
                "plugin": reviewed.plugin,
                "options": deep_thaw(reviewed.options),
                "on_success": topology_sources[reviewed.name]["on_success"],
                "on_validation_failure": reviewed.on_validation_failure,
            }
            for stable_id in guided.source_order
            for reviewed in (guided.reviewed_sources[stable_id],)
        },
        "nodes": deepcopy(topology["nodes"]),
        "edges": deepcopy(topology["edges"]),
        "outputs": [
            {
                "sink_name": reviewed.name,
                "plugin": reviewed.plugin,
                "options": deep_thaw(reviewed.options),
                "on_write_failure": reviewed.on_write_failure,
            }
            for stable_id in guided.output_order
            for reviewed in (guided.reviewed_outputs[stable_id],)
        ],
    }


def _set_pipeline_document(state: CompositionState) -> dict[str, object]:
    document = state.to_dict()
    document.pop("version")
    outputs = document["outputs"]
    assert type(outputs) is list
    document["outputs"] = [
        {"sink_name": output["name"], **{key: value for key, value in output.items() if key != "name"}} for output in outputs
    ]
    return document


def _boundary_valid_correction_predecessor() -> CompositionState:
    return CompositionState(
        sources={
            "source": SourceSpec(
                plugin="csv",
                on_success="amount_gate",
                options={"path": "source.csv", "schema": {"mode": "fixed", "fields": ["amount: int"]}},
                on_validation_failure="discard",
            )
        },
        nodes=(
            NodeSpec(
                id="amount_gate",
                node_type="gate",
                plugin=None,
                input="amount_gate",
                on_success=None,
                on_error=None,
                options={"schema": {"mode": "fixed", "fields": ["amount: int"]}},
                condition="row['amount'] > 500",
                routes={"true": "high_value", "false": "high_value"},
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
            NodeSpec(
                id="summarize_standard",
                node_type="transform",
                plugin="passthrough",
                input="high_value",
                on_success="format_high_value_input",
                on_error="discard",
                options={"schema": {"mode": "fixed", "fields": ["amount: int"]}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
            NodeSpec(
                id="format_high_value",
                node_type="transform",
                plugin="field_mapper",
                input="format_high_value_input",
                on_success="output",
                on_error="discard",
                options={
                    "mapping": {"amount": "amount", "tier": "'high'"},
                    "select_only": False,
                    "schema": {"mode": "observed", "required_fields": ["amount"]},
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(
                name="output",
                plugin="json",
                options={"path": "outputs/output.jsonl", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def _assert_materialized_and_full_candidates_build_the_same_state(
    *,
    delta: dict[str, object],
    authorized_full_candidate: dict[str, object],
    guided: GuidedSession,
    current_state: CompositionState,
    authority: GuidedCorrectionTarget | None = None,
) -> None:
    materialized = materialize_guided_authorized_candidate(
        delta,
        authority=authority,
        guided=guided,
        current_state=current_state,
    )
    context = _candidate_context()
    delta_candidate = build_set_pipeline_candidate(materialized, current_state, context)
    full_candidate = build_set_pipeline_candidate(authorized_full_candidate, current_state, context)
    assert delta_candidate.acceptable, [entry.message for entry in delta_candidate.result.validation.errors]
    assert full_candidate.acceptable, [entry.message for entry in full_candidate.result.validation.errors]
    assert delta_candidate.result.updated_state == full_candidate.result.updated_state


def test_initial_linear_delta_matches_equivalent_full_candidate_state() -> None:
    guided = _boundary_valid_guided()
    topology = _linear_pipeline()
    delta = {
        "source_routes": [{"stable_id": SOURCE_ID, "on_success": "output"}],
        "nodes": [],
        "edges": [],
        "output_targets": [{"stable_id": OUTPUT_ID}],
    }
    _assert_materialized_and_full_candidates_build_the_same_state(
        delta=delta,
        authorized_full_candidate=_authorized_initial_full_candidate(topology, guided),
        guided=guided,
        current_state=_empty_candidate_state(),
    )


def test_initial_fork_coalesce_delta_matches_equivalent_full_candidate_state() -> None:
    guided = _boundary_valid_guided()
    topology = _fork_coalesce_pipeline()
    topology_sources = topology["sources"]
    assert type(topology_sources) is dict
    delta = {
        "source_routes": [{"stable_id": SOURCE_ID, "on_success": topology_sources["source"]["on_success"]}],
        "nodes": deepcopy(topology["nodes"]),
        "edges": deepcopy(topology["edges"]),
        "output_targets": [{"stable_id": OUTPUT_ID}],
    }
    _assert_materialized_and_full_candidates_build_the_same_state(
        delta=delta,
        authorized_full_candidate=_authorized_initial_full_candidate(topology, guided),
        guided=guided,
        current_state=_empty_candidate_state(),
    )


def test_initial_multi_output_delta_matches_equivalent_full_candidate_state() -> None:
    guided = _boundary_valid_guided(multiple_outputs=True)
    topology = _two_output_candidate("output", "quarantine")
    delta = {
        "source_routes": [{"stable_id": SOURCE_ID, "on_success": "triage"}],
        "nodes": deepcopy(topology["nodes"]),
        "edges": deepcopy(topology["edges"]),
        "output_targets": [{"stable_id": OUTPUT_ID}, {"stable_id": OUTPUT_B_ID}],
    }
    _assert_materialized_and_full_candidates_build_the_same_state(
        delta=delta,
        authorized_full_candidate=_authorized_initial_full_candidate(topology, guided),
        guided=guided,
        current_state=_empty_candidate_state(),
    )


def test_selected_source_correction_delta_matches_equivalent_full_candidate_state() -> None:
    guided = _boundary_valid_correction_guided()
    predecessor = _boundary_valid_correction_predecessor()
    added_node = {
        "id": "screen_rows",
        "node_type": "transform",
        "plugin": "passthrough",
        "input": "screen_input",
        "on_success": "amount_gate",
        "on_error": "discard",
        "options": {"schema": {"mode": "observed"}},
    }
    added_edge = {
        "id": "source_to_screen",
        "from_node": "source",
        "to_node": "screen_rows",
        "edge_type": "on_success",
    }
    delta = {
        "source_routes": [{"stable_id": SOURCE_ID, "on_success": "screen_input"}],
        "nodes": [added_node],
        "edges": [added_edge],
    }
    full = _set_pipeline_document(predecessor)
    full["sources"]["source"]["on_success"] = "screen_input"
    full["nodes"].append(deepcopy(added_node))
    full["edges"] = [deepcopy(added_edge)]
    _assert_materialized_and_full_candidates_build_the_same_state(
        delta=delta,
        authorized_full_candidate=full,
        guided=guided,
        current_state=predecessor,
        authority=_source_correction_target(),
    )


def test_selected_node_correction_delta_matches_equivalent_full_candidate_state() -> None:
    guided = _boundary_valid_correction_guided()
    predecessor = _boundary_valid_correction_predecessor()
    selected = deepcopy(_planner_correction_candidate()["nodes"][2])
    delta = {
        "node_patch": {
            "stable_id": _format_node_correction_target().requested.stable_id,
            "options": {
                "mapping": selected["options"]["mapping"],
                "select_only": selected["options"]["select_only"],
            },
        },
        "edges": [],
    }
    full = _set_pipeline_document(predecessor)
    full["nodes"][2]["options"] = {
        "mapping": {"amount": "amount", "tier": "'priority'"},
        "select_only": True,
        "schema": {"mode": "observed", "required_fields": ["amount"]},
    }
    _assert_materialized_and_full_candidates_build_the_same_state(
        delta=delta,
        authorized_full_candidate=full,
        guided=guided,
        current_state=predecessor,
        authority=_format_node_correction_target(),
    )


def test_output_upstream_reconnection_delta_matches_equivalent_full_candidate_state() -> None:
    guided = _boundary_valid_correction_guided()
    predecessor = _boundary_valid_correction_predecessor()
    edge = {
        "id": "summarize_error_to_output",
        "from_node": "summarize_standard",
        "to_node": "output",
        "edge_type": "on_error",
    }
    delta = {
        "output_targets": [{"stable_id": OUTPUT_ID}],
        "edges": [edge],
    }
    full = _set_pipeline_document(predecessor)
    full["nodes"][1]["on_error"] = "output"
    full["edges"] = [deepcopy(edge)]
    _assert_materialized_and_full_candidates_build_the_same_state(
        delta=delta,
        authorized_full_candidate=full,
        guided=guided,
        current_state=predecessor,
        authority=_output_correction_target(),
    )

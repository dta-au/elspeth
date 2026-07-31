"""Bind remaps planner-invented component references onto reviewed authority.

The planner authors topology against the redacted reviewed context. When it
invents its own output name anyway, ``bind_guided_reviewed_components`` must
restore the reviewed name AND rewrite every reference to the invented name —
source/node ``on_success``/``on_error`` routing and the ``edges`` array — or
the candidate dies at set_pipeline validation with "unknown node" and the
repair loop burns to REPAIR_EXHAUSTED (elspeth-859e2702dd).
"""

from __future__ import annotations

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.guided.planning import bind_guided_reviewed_components
from elspeth.web.composer.guided.protocol import GuidedStep
from elspeth.web.composer.guided.resolved import SinkOutputResolved, SourceResolved
from elspeth.web.composer.guided.state_machine import GuidedSession

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

    with pytest.raises(AuditIntegrityError, match="does not identify reviewed sources"):
        bind_guided_reviewed_components(pipeline, _guided())


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


def test_bind_resolves_dangling_sink_reference_to_single_reviewed_output() -> None:
    # Planner slip observed live: outputs and edges correctly use the reviewed
    # name, but one stale invented name ('csv_rows') survives in on_success.
    # With exactly one reviewed output the reference is unambiguous — resolve
    # it structurally instead of letting validation reject a repair the
    # planner cannot see through the closed feedback.
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

    bound = bind_guided_reviewed_components(pipeline, _guided())

    assert bound["sources"]["source"]["on_success"] == "output"


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

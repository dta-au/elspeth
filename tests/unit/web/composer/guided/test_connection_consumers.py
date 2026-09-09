"""Canonical consumer projection for guided arbitrary-DAG proposals."""

from __future__ import annotations

from elspeth.web.composer.guided.connection_consumers import canonical_connection_consumers
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec


def test_row_union_consumes_every_branch_once_without_claiming_its_input_placeholder() -> None:
    state = CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="routed",
            options={"schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(
            NodeSpec(
                id="variants",
                node_type="row_union",
                plugin=None,
                input="control_done",
                on_success="experiment_rows",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches={
                    "control_branch": "control_done",
                    "treatment_branch": "treatment_done",
                },
                policy=None,
                merge=None,
                timeout_seconds=15.0,
            ),
            NodeSpec(
                id="compare",
                node_type="aggregation",
                plugin="batch_experiment_compare",
                input="experiment_rows",
                on_success="results",
                on_error="discard",
                options={
                    "schema": {"mode": "observed"},
                    "variant_field": "prompt_variant",
                    "score_field": "score",
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
                trigger={},
                output_mode="transform",
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(
                name="results",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )

    consumers = canonical_connection_consumers(
        state,
        node_identities={"variants": "stable-union", "compare": "stable-compare"},
        output_identities={"results": "stable-output"},
    )

    assert consumers["control_done"] == (("node", "stable-union"),)
    assert consumers["treatment_done"] == (("node", "stable-union"),)
    assert consumers["experiment_rows"] == (("node", "stable-compare"),)
    assert consumers["results"] == (("output", "stable-output"),)

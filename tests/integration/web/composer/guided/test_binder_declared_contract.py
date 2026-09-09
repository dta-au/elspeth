"""Declared output fields become an enforced contract at the binder seam (F3).

Step-2 field review captures ``SinkOutputResolved.required_fields``, but both
the composer sink-contract check and the runtime DAG validation key off the
sink's ``options.schema`` — until the binder materializes the declared fields
there, the operator's declared contract is display-only.  These tests run the
real candidate boundary on the BOUND pipeline: a fixed-schema source that
cannot satisfy a declared field must reject the nodeless candidate — and the
rejection must reach the exact repair feedback the planner sees — while the
legitimate observed-source + select_only field_mapper shape stays valid.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.guided.planning import bind_guided_reviewed_components
from elspeth.web.composer.guided.protocol import GuidedStep
from elspeth.web.composer.guided.resolved import SinkOutputResolved, SourceResolved
from elspeth.web.composer.guided.state_machine import GuidedSession
from elspeth.web.composer.pipeline_planner import _allowlisted_candidate_feedback
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools import ToolContext, build_set_pipeline_candidate
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

SOURCE_ID = "00000000-0000-4000-8000-000000000601"
OUTPUT_ID = "00000000-0000-4000-8000-000000000602"


def _context(tmp_path: Path) -> ToolContext:
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return ToolContext(
        catalog=PolicyCatalogView.for_trained_operator(catalog, snapshot),
        plugin_snapshot=snapshot,
        data_dir=str(tmp_path),
        session_id="test-session",
    )


def _empty_state() -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _guided(
    tmp_path: Path,
    *,
    source_schema: dict[str, Any],
    declared_fields: tuple[str, ...],
) -> GuidedSession:
    return GuidedSession(
        step=GuidedStep.STEP_3_TRANSFORMS,
        source_order=(SOURCE_ID,),
        reviewed_sources={
            SOURCE_ID: SourceResolved(
                name="source",
                plugin="csv",
                options={
                    "path": str(tmp_path / "blobs" / "test-session" / "input.csv"),
                    "schema": source_schema,
                },
                observed_columns=("name",),
                sample_rows=(),
                on_validation_failure="discard",
            )
        },
        output_order=(OUTPUT_ID,),
        reviewed_outputs={
            OUTPUT_ID: SinkOutputResolved(
                name="output",
                plugin="json",
                options={
                    "path": "outputs/result.jsonl",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                    "format": "jsonl",
                },
                required_fields=declared_fields,
                schema_mode="observed",
                on_write_failure="discard",
            )
        },
    )


def test_unsatisfiable_declared_field_rejects_the_nodeless_candidate_with_repair_feedback(tmp_path: Path) -> None:
    # F5/P3 shape: fixed-schema source guarantees only 'name', the operator
    # declared 'email' at Step-2 field review, and the planner proposed a
    # nodeless pass-through. Before fix 3a the declared field never reached
    # the sink options, both contract checks abstained, and the impossible
    # pipeline sailed through to a per-row runtime failure.
    guided = _guided(
        tmp_path,
        source_schema={"mode": "fixed", "fields": ["name: str"]},
        declared_fields=("email",),
    )
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
        "edges": [],
        "outputs": [
            {"sink_name": "output", "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }

    bound = bind_guided_reviewed_components(pipeline, guided)
    candidate = build_set_pipeline_candidate(bound, _empty_state(), _context(tmp_path))

    assert not candidate.acceptable
    codes = [entry.error_code for entry in candidate.result.validation.errors]
    assert "sink_contract_violation" in codes
    # The rejection must reach the exact repair feedback projection the
    # planner receives — a rejection the repair loop never sees is display-only.
    feedback = _allowlisted_candidate_feedback(candidate.result)
    feedback_codes = [entry["error_code"] for entry in feedback["validation"]["errors"]]
    assert "sink_contract_violation" in feedback_codes


def test_observed_source_with_select_only_mapper_and_declared_fields_stays_valid(tmp_path: Path) -> None:
    # REGRESSION (P1 shape): an observed CSV source feeding a select_only
    # field_mapper that emits exactly the declared fields is a legitimate
    # pipeline — materializing the declared contract must not false-block it.
    guided = _guided(
        tmp_path,
        source_schema={"mode": "observed"},
        declared_fields=("name", "score"),
    )
    pipeline = {
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
                "id": "shape",
                "node_type": "transform",
                "plugin": "field_mapper",
                "input": "rows",
                "on_success": "output",
                "on_error": "discard",
                "options": {
                    "mapping": {"name": "name", "score": "score"},
                    "select_only": True,
                    "schema": {"mode": "observed"},
                },
            }
        ],
        "edges": [],
        "outputs": [
            {"sink_name": "output", "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }

    bound = bind_guided_reviewed_components(pipeline, guided)

    # The declared contract is present in the bound sink options...
    assert bound["outputs"][0]["options"]["schema"]["required_fields"] == ["name", "score"]

    candidate = build_set_pipeline_candidate(bound, _empty_state(), _context(tmp_path))

    # ...and the legitimate shape still validates end to end.
    assert candidate.acceptable, [entry.message for entry in candidate.result.validation.errors]

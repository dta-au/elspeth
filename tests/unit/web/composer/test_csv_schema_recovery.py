"""CSV rejection repair through the real Composer loop and tool dispatch."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy import select

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.advisor_decision import AdvisorGateBlocked
from elspeth.web.composer.service import ComposerAvailability
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools import execute_tool
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.models import blobs_table, composition_rejection_events_table
from elspeth.web.sessions.protocol import CompositionStateData
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.composer._helpers import _composer_service_with_session, _make_llm_response, _make_settings


def _scaffold(template: Path, output: Path) -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="qa_report_scaffold",
            on_validation_failure="discard",
            options={"path": str(template), "schema": {"mode": "observed"}},
        ),
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(
                name="qa_report_scaffold",
                plugin="csv",
                options={
                    "path": str(output),
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(name="Incomplete assessment scaffold"),
        version=2,
    )


def _assessment(case_studies: Path, output: Path) -> dict[str, Any]:
    return {
        "source": {
            "plugin": "csv",
            "on_success": "cases",
            "on_validation_failure": "discard",
            "options": {"path": str(case_studies), "schema": {"mode": "flexible", "fields": ["case_study: str"]}},
        },
        "nodes": [
            {
                "id": "assess",
                "node_type": "transform",
                "plugin": "llm",
                "input": "cases",
                "on_success": "qa_report",
                "on_error": "discard",
                "options": {
                    "provider": "bedrock",
                    "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
                    "region_name": "us-east-1",
                    "system_prompt": "You assess case-study quality against the supplied rubric. Explain the assessment concisely.",
                    "prompt_template": "Assess the quality of this case study: {{ row.case_study }}",
                    "required_input_fields": ["case_study"],
                    "response_field": "assessment",
                    "schema": {"mode": "observed"},
                },
            }
        ],
        "edges": [],
        "outputs": [
            {
                "sink_name": "qa_report",
                "plugin": "csv",
                "options": {
                    "path": str(output),
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        ],
        "metadata": {"name": "Case study quality assessment"},
    }


def _feedback(messages: list[dict[str, Any]], call_id: str) -> dict[str, Any]:
    response = next(message for message in messages if message["role"] == "tool" and message["tool_call_id"] == call_id)
    return json.loads(response["content"])


@pytest.mark.asyncio
@pytest.mark.parametrize("uploaded", [False, True], ids=["path_bound", "uploaded"])
async def test_csv_schema_rejections_repair_without_replacing_prior_scaffold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uploaded: bool
) -> None:
    """Both rejected proposals survive to the provider as actionable feedback.

    Only the external model is scripted. Catalogue admission, source config
    validation, dispatch, state settlement and durable writes are production.
    """

    def available(*, model: str, **_kwargs: object) -> ComposerAvailability:
        return ComposerAvailability(available=True, model=model, provider="test")

    monkeypatch.setattr("elspeth.web.composer.service.compute_availability", available)
    catalog = create_catalog_service()
    service, session_id = _composer_service_with_session(catalog=catalog, settings=_make_settings(data_dir=tmp_path))
    sessions = service._require_sessions_service()
    blob_dir = tmp_path / "blobs" / session_id
    output_dir = tmp_path / "outputs" / session_id
    csv_files = {
        "case-studies.csv": b"Case Study _\nA synthetic case study\n",
        "report-template.csv": b"assessment\n\n",
        "column-guide.csv": b"column,meaning\nCase Study _,Case to assess\n",
    }
    markdown_files = {
        f"{name}.md": f"Synthetic {name} for quality assessment.\n".encode() for name in ("instructions", "context", "rubric", "examples")
    }
    case_blob_id = None
    template_blob_id = None
    if uploaded:
        blobs = BlobServiceImpl(sessions._engine, tmp_path)
        records = {}
        with fenced_operation_context(sessions._engine, session_id, operation_kind=SessionOperationKind.CREATE) as upload_context:
            for filename, content in csv_files.items():
                records[filename] = await blobs.create_blob(
                    UUID(session_id), filename, content, "text/csv", session_operation_context=upload_context
                )
            for filename, content in markdown_files.items():
                records[filename] = await blobs.create_blob(
                    UUID(session_id), filename, content, "text/plain", session_operation_context=upload_context
                )
        with sessions._engine.connect() as conn:
            uploaded_rows = conn.execute(
                select(blobs_table.c.filename, blobs_table.c.status, blobs_table.c.created_by).where(blobs_table.c.session_id == session_id)
            ).all()
        assert set(uploaded_rows) == {(filename, "ready", "user") for filename in csv_files | markdown_files}
        case_blob_id = str(records["case-studies.csv"].id)
        template_blob_id = str(records["report-template.csv"].id)
        cases = Path(records["case-studies.csv"].storage_path)
        template = Path(records["report-template.csv"].storage_path)
    else:
        blob_dir.mkdir(parents=True)
        for filename, content in (csv_files | markdown_files).items():
            (blob_dir / filename).write_bytes(content)
        cases = blob_dir / "case-studies.csv"
        template = blob_dir / "report-template.csv"
    scaffold = _scaffold(template, output_dir / "scaffold.csv")
    if uploaded:
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
        with fenced_operation_context(sessions._engine, session_id) as binding_context:
            bound = execute_tool(
                "set_source_from_blob",
                {
                    "blob_id": template_blob_id,
                    "on_success": "qa_report_scaffold",
                    "on_validation_failure": "discard",
                    "options": {"schema": {"mode": "observed"}},
                },
                scaffold,
                PolicyCatalogView.for_trained_operator(catalog, snapshot),
                plugin_snapshot=snapshot,
                data_dir=str(tmp_path),
                session_engine=sessions._engine,
                session_id=session_id,
                session_operation_context=binding_context,
                session_operation_authority=sessions.session_operation_authority,
                validate_arguments=True,
                require_data_dir_for_paths=True,
            )
        assert bound.success, bound.to_dict()
        scaffold = bound.updated_state
        assert scaffold.sources["source"].options["blob_ref"] == template_blob_id
    scaffold_data = scaffold.to_dict()
    saved_scaffold = await sessions.save_composition_state(
        UUID(session_id),
        CompositionStateData(
            sources=scaffold_data["sources"],
            nodes=scaffold_data["nodes"],
            edges=scaffold_data["edges"],
            outputs=scaffold_data["outputs"],
            metadata_=scaffold_data["metadata"],
            is_valid=True,
            validation_errors=(),
        ),
        provenance="session_seed",
    )
    repaired = _assessment(cases, output_dir / "assessment.csv")
    if uploaded:
        repaired["source"]["blob_id"] = case_blob_id
        del repaired["source"]["options"]["path"]
    invalid_name = deepcopy(repaired)
    invalid_name["source"]["options"]["schema"]["fields"] = ["case_study_: str"]
    unsupported_stamp = deepcopy(repaired)
    unsupported_stamp["source"]["options"]["schema"] = {"mode": "observed", "guaranteed_fields": ["case_study"]}
    proposals = [invalid_name, unsupported_stamp, repaired]
    primary_calls = 0
    advisor_calls = 0

    async def complete(**kwargs: Any):
        nonlocal primary_calls, advisor_calls
        if kwargs["model"] == service._settings.composer_advisor_model:
            advisor_calls += 1
            return _make_llm_response(
                content=json.dumps({"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None})
            )
        messages = kwargs["messages"]
        if primary_calls in (1, 2):
            current = await sessions.get_current_state(UUID(session_id))
            assert current is not None
            assert current.id == saved_scaffold.id
            assert current.sources == saved_scaffold.sources
            assert current.nodes == ()
        if primary_calls == 1:
            first = _feedback(messages, "invalid_name")
            assert first["success"] is False
            errors = [entry for entry in first["validation"]["errors"] if entry.get("error_code") == "plugin_options_invalid"]
            assert errors, json.dumps(first, indent=2)
            error = errors[0]
            assert "'case_study_' -> 'case_study'" in error["message"]
            assert "field_mapping: {case_study: case_study_}" in error["message"]
        if primary_calls == 2:
            second = _feedback(messages, "unsupported_stamp")
            assert second["success"] is False
            errors = [entry for entry in second["validation"]["errors"] if entry.get("error_code") == "source_data_contract_required"]
            assert errors, json.dumps(second, indent=2)
            error = errors[0]
            assert "request_interpretation_review" in error["message"]
            assert "flexible" in error["message"]
            guidance = second["validation_guidance"]["codes"]["source_data_contract_required"]
            assert "source_data_contract" in json.dumps(guidance)
            assert "fields" in json.dumps(guidance)
        index = primary_calls
        primary_calls += 1
        if index < len(proposals):
            return _make_llm_response(
                tool_calls=[
                    {"id": ("invalid_name", "unsupported_stamp", "repaired")[index], "name": "set_pipeline", "arguments": proposals[index]}
                ]
            )
        if index == 3:
            assert _feedback(messages, "repaired")["success"] is True
            return _make_llm_response(
                tool_calls=[
                    {
                        "id": "review_model_choice",
                        "name": "request_interpretation_review",
                        "arguments": {"affected_node_id": "assess", "kind": "llm_model_choice", "user_term": "llm_model_choice:assess"},
                    }
                ]
            )
        assert index == 4, json.dumps(messages[-3:], indent=2)
        assert not kwargs.get("tools"), "A staged review finishes through the provider's reply-only turn"
        return _make_llm_response(
            content="The assessment graph now reads case studies and evaluates each row; required reviews remain before execution."
        )

    with patch("litellm.acompletion", side_effect=complete):
        result = await service.compose(
            "Build an LLM quality assessment for the case-study CSV using the instructions, context, rubric, examples, report template and Column Guide.",
            [],
            scaffold,
            session_id=session_id,
            current_state_id=str(saved_scaffold.id),
        )

    assert primary_calls == 5
    assert advisor_calls == 0, "Model-choice acknowledgement precedes completion review"
    assert result.state.sources["source"].options["path"] == str(cases)
    assert result.state.sources["source"].options["schema"]["fields"] == ("case_study: str",)
    assert "guaranteed_fields" not in result.state.sources["source"].options["schema"]
    assert [(node.id, node.plugin) for node in result.state.nodes] == [("assess", "llm")]
    assert result.state.nodes[0].options["required_input_fields"] == ("case_study",)
    final = await sessions.get_current_state(UUID(session_id))
    assert final is not None
    assert final.id != saved_scaffold.id
    assert final.sources["source"]["options"]["path"] == str(cases)
    if uploaded:
        assert final.sources["source"]["options"]["blob_ref"] == case_blob_id
    assert [node["plugin"] for node in final.nodes] == ["llm"]
    pending = await sessions.list_interpretation_events(UUID(session_id), status="pending")
    assert any(event.user_term == "llm_model_choice:assess" for event in pending)
    with sessions._engine.connect() as conn:
        rejections = conn.execute(
            select(composition_rejection_events_table.c.tool_call_id, composition_rejection_events_table.c.error_code).where(
                composition_rejection_events_table.c.session_id == session_id
            )
        ).all()
    assert set(rejections) == {("invalid_name", "plugin_options_invalid"), ("unsupported_stamp", "source_data_contract_required")}
    assert result.runtime_preflight is not None
    assert result.runtime_preflight.readiness.authoring_valid is True
    assert result.runtime_preflight.readiness.execution_ready is False


@pytest.mark.asyncio
async def test_template_scaffold_cannot_satisfy_assessment_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid report scaffold still requires the requested assessment work."""

    def available(*, model: str, **_kwargs: object) -> ComposerAvailability:
        return ComposerAvailability(available=True, model=model, provider="test")

    monkeypatch.setattr("elspeth.web.composer.service.compute_availability", available)
    service, session_id = _composer_service_with_session(
        catalog=create_catalog_service(),
        settings=_make_settings(data_dir=tmp_path, composer_advisor_checkpoint_max_passes=1),
    )
    template = tmp_path / "blobs" / session_id / "report-template.csv"
    template.parent.mkdir(parents=True)
    template.write_text("assessment\n\n", encoding="utf-8")
    state = _scaffold(template, tmp_path / "outputs" / session_id / "scaffold.csv")
    advisor_calls = 0

    async def complete(**kwargs: Any):
        nonlocal advisor_calls
        if kwargs["model"] == service._settings.composer_advisor_model:
            advisor_calls += 1
            assert "assess" in json.dumps(kwargs["messages"]).lower()
            return _make_llm_response(
                content=json.dumps(
                    {
                        "verdict": "FLAGGED",
                        "category": "other",
                        "steps": [],
                        "findings": "The report scaffold has no assessment transforms and reads the report template instead of case studies.",
                        "note": "Build the assessment transforms and bind the case-study input before claiming completion.",
                    }
                )
            )
        return _make_llm_response(content="The assessment pipeline is complete.")

    with patch("litellm.acompletion", side_effect=complete):
        result = await service.compose("Build an LLM assessment of every case study.", [], state, session_id=session_id)

    assert advisor_calls == 1
    assert result.state == state
    assert isinstance(result.advisor_gate_decision, AdvisorGateBlocked)
    assert result.runtime_preflight is not None
    assert result.runtime_preflight.readiness.completion_ready is False
    assert result.advisor_gate_decision.fact.note is not None
    assert "case-study" in result.advisor_gate_decision.fact.note
    assert "completion advisory review did not clear" in result.message.lower()
    assert "assessment" in result.message.lower()

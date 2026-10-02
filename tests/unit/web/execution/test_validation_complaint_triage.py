"""Real graph checks for the complaint classifier and SLA lookup session."""

from pathlib import Path

import pytest
from pydantic import SecretBytes

from elspeth.web.composer import yaml_generator
from elspeth.web.composer.state import CompositionState
from elspeth.web.config import WebSettings
from elspeth.web.execution.validation import validate_pipeline_for_trained_operator


def complaint_triage_state(*, pending: bool, narrow_consumer: bool = True) -> CompositionState:
    """The live session's contracts, with deterministic local fixture data."""
    requirements = (
        [
            {
                "id": "category:classify_category",
                "kind": "vague_term",
                "user_term": "category",
                "status": "pending",
                "draft": "billing, outage, or other",
            },
            {
                "id": "prompt_template_review:classify_category",
                "kind": "llm_prompt_template",
                "user_term": "llm_prompt_template:classify_category",
                "status": "pending",
                "draft": "Classify {{ row.complaint }}",
            },
        ]
        if pending
        else []
    )
    consumer_type = "str" if narrow_consumer else "any"
    return CompositionState.from_dict(
        {
            "sources": {
                "source": {
                    "plugin": "csv",
                    "on_success": "complaints",
                    "on_validation_failure": "discard",
                    "options": {
                        "path": "blobs/test-session/input.csv",
                        "schema": {"mode": "flexible", "fields": ["id: str", "complaint: str"]},
                    },
                }
            },
            "nodes": [
                {
                    "id": "classify_category",
                    "node_type": "transform",
                    "plugin": "llm",
                    "input": "complaints",
                    "on_success": "classified",
                    "on_error": "discard",
                    "options": {
                        "provider": "openrouter",
                        "model": "openai/gpt-4o",
                        "api_key": "test-key",
                        "prompt_template": "Classify {{ row.complaint }}",
                        "required_input_fields": ["complaint"],
                        "response_format": "structured",
                        "output_fields": [{"suffix": "category", "type": "enum", "values": ["billing", "outage", "other"]}],
                        "schema": {"mode": "observed", "guaranteed_fields": ["id", "complaint", "category"]},
                        "interpretation_requirements": requirements,
                    },
                },
                {
                    "id": "attach_sla",
                    "node_type": "transform",
                    "plugin": "reference_join",
                    "input": "classified",
                    "on_success": "sla_attached",
                    "on_error": "discard",
                    "options": {
                        "schema": {"mode": "observed", "guaranteed_fields": ["id", "complaint", "category", "response_sla_hours"]},
                        "required_input_fields": ["category"],
                        "reference_content": "category,response_sla_hours\nbilling,24\noutage,4\nother,48\n",
                        "reference_format": "csv",
                        "key_field": "category",
                        "reference_key_name": "category",
                        "output": {"response_sla_hours": "ref['response_sla_hours']"},
                        "on_miss": "fail",
                    },
                },
                {
                    "id": "tidy_columns",
                    "node_type": "transform",
                    "plugin": "field_mapper",
                    "input": "sla_attached",
                    "on_success": "results",
                    "on_error": "discard",
                    "options": {
                        "schema": {
                            "mode": "flexible",
                            "fields": [
                                f"id: {consumer_type}",
                                f"complaint: {consumer_type}",
                                f"category: {consumer_type}",
                                "response_sla_hours: str",
                            ],
                        },
                        "mapping": {name: name for name in ("id", "complaint", "category", "response_sla_hours")},
                        "select_only": True,
                    },
                },
            ],
            "edges": [],
            "outputs": [
                {
                    "name": "results",
                    "plugin": "csv",
                    "options": {"path": "outputs/results.csv", "schema": {"mode": "observed"}},
                    "on_write_failure": "discard",
                }
            ],
            "metadata": {"name": None, "description": None},
            "version": 1,
        }
    )


def complaint_settings(data_dir: Path) -> WebSettings:
    source_dir = data_dir / "blobs" / "test-session"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "input.csv").write_text("id,complaint\nC-1,My bill is wrong\n")
    return WebSettings(
        data_dir=data_dir,
        composer_max_composition_turns=10,
        composer_max_discovery_turns=5,
        composer_timeout_seconds=240.0,
        composer_rate_limit_per_minute=60,
        shareable_link_signing_key=SecretBytes(b"\x00" * 32),
    )


@pytest.mark.parametrize("narrow_consumer", [True, False])
def test_complaint_join_graph_failure_names_authored_patch_targets(tmp_path: Path, narrow_consumer: bool) -> None:
    state = complaint_triage_state(pending=True, narrow_consumer=narrow_consumer)
    result = validate_pipeline_for_trained_operator(
        state,
        complaint_settings(tmp_path),
        yaml_generator,
        session_id="test-session",
        allow_pending_interpretation_placeholders=True,
    )
    assert result.is_valid is not narrow_consumer
    if narrow_consumer:
        error = result.errors[0]
        assert error.component_id == "tidy_columns"
        assert "producer node 'attach_sla'" in error.message
        assert "consumer node 'tidy_columns'" in error.message
        assert error.suggestion is not None
        assert "patch_node_options(node_id='tidy_columns'" in error.suggestion
        assert "patch_node_options(node_id='attach_sla'" in error.suggestion
        assert "patch_node_options(node_id='transform_" not in error.suggestion

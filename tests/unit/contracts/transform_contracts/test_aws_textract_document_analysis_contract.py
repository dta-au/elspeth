"""ADR-009 forward-invariant contract for Amazon Textract enrichment."""

from __future__ import annotations

from unittest.mock import Mock

from elspeth.contracts import Determinism
from elspeth.plugins.transforms.aws.textract_document_analysis import AWSTextractDocumentAnalysis
from elspeth.testing import make_pipeline_row


def _probe_transform() -> AWSTextractDocumentAnalysis:
    return AWSTextractDocumentAnalysis(AWSTextractDocumentAnalysis.probe_config())


def test_transform_declares_external_call_passthrough() -> None:
    transform = _probe_transform()

    assert transform.name == "aws_textract_document_analysis"
    assert transform.determinism is Determinism.EXTERNAL_CALL
    assert transform.passes_through_input is True
    assert transform.input_schema is not None
    assert transform.output_schema is not None


def test_forward_invariant_probe_enriches_and_passes_through() -> None:
    transform = _probe_transform()
    rows = transform.forward_invariant_probe_rows(make_pipeline_row({"existing_field": "keep-me"}))
    assert len(rows) == 1

    ctx = Mock(spec_set=["state_id", "token", "landscape", "run_id", "telemetry_emit"])
    ctx.state_id = "probe-state"
    ctx.token = None
    ctx.landscape = Mock()
    ctx.run_id = "probe-run"
    ctx.telemetry_emit = lambda _event: None

    result = transform.execute_forward_invariant_probe(rows, ctx)

    assert result.status == "success"
    output = result.row.to_dict()
    assert output["textract_text"] == "probe"
    assert output["document_bucket"] == "probe-bucket"
    assert output["document_key"] == "probe-document.pdf"
    assert output["existing_field"] == "keep-me"


def test_forward_invariant_probe_success_reason_action() -> None:
    transform = _probe_transform()
    rows = transform.forward_invariant_probe_rows(make_pipeline_row({}))
    ctx = Mock(spec_set=["state_id", "token", "landscape", "run_id", "telemetry_emit"])
    ctx.state_id = "probe-state"
    ctx.token = None
    ctx.landscape = Mock()
    ctx.run_id = "probe-run"
    ctx.telemetry_emit = lambda _event: None

    result = transform.execute_forward_invariant_probe(rows, ctx)

    assert result.success_reason["action"] == "enriched"

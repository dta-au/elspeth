"""ADR-009 forward-invariant contract tests for azure_content_understanding.

The transform is ``passes_through_input=True`` and uses BatchTransformMixin, so it
is covered by the forward invariant (not the backward probe). These tests exercise
the real per-row LRO path via ``execute_forward_invariant_probe`` (which drives
``_process_single_with_state`` with a local probe client), asserting the row is
enriched AND every input field is preserved.
"""

from __future__ import annotations

from unittest.mock import Mock

from elspeth.contracts import Determinism
from elspeth.plugins.transforms.azure.content_understanding import AzureContentUnderstanding
from elspeth.testing import make_pipeline_row


def _probe_transform() -> AzureContentUnderstanding:
    return AzureContentUnderstanding(AzureContentUnderstanding.probe_config())


def _ctx() -> Mock:
    ctx = Mock(spec_set=["state_id", "token"])
    ctx.state_id = "probe-state"
    ctx.token = None
    return ctx


def test_transform_declares_external_call_passthrough() -> None:
    t = _probe_transform()
    assert t.name == "azure_content_understanding"
    assert t.determinism is Determinism.EXTERNAL_CALL
    assert t.passes_through_input is True
    assert t.input_schema is not None
    assert t.output_schema is not None


def test_forward_invariant_probe_enriches_and_passes_through() -> None:
    t = _probe_transform()
    rows = t.forward_invariant_probe_rows(make_pipeline_row({"existing_field": "keep-me"}))
    assert len(rows) == 1

    result = t.execute_forward_invariant_probe(rows, _ctx())

    assert result.status == "success"
    out = result.row.to_dict()
    assert out["cu_markdown"] == "probe"
    assert out[t._source_field] == "https://probe.example/doc.pdf"
    assert out["existing_field"] == "keep-me"
    assert result.success_reason["action"] == "enriched"


def test_forward_invariant_probe_in_base64_mode() -> None:
    config = {**AzureContentUnderstanding.probe_config(), "source_mode": "base64"}
    t = AzureContentUnderstanding(config)
    rows = t.forward_invariant_probe_rows(make_pipeline_row({}))
    result = t.execute_forward_invariant_probe(rows, _ctx())
    assert result.status == "success"
    assert result.row.to_dict()["cu_markdown"] == "probe"

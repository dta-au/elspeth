"""ADR-009 forward-invariant contract for Amazon Textract inline enrichment."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from elspeth.contracts import Determinism
from elspeth.plugins.transforms.aws.textract_inline_analysis import AWSTextractInlineAnalysis
from elspeth.testing import make_pipeline_row


def _probe_transform() -> AWSTextractInlineAnalysis:
    return AWSTextractInlineAnalysis(AWSTextractInlineAnalysis.probe_config())


@dataclass
class _ProbeAuditWriter:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def allocate_call_index(self, state_id: str) -> int:
        del state_id
        return len(self.calls)

    def record_call(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(call_id=f"probe-call-{len(self.calls)}")


def _probe_context() -> SimpleNamespace:
    return SimpleNamespace(
        state_id="probe-state",
        token=None,
        landscape=_ProbeAuditWriter(),
        run_id="probe-run",
        telemetry_emit=lambda _event: None,
    )


def test_transform_declares_external_call_passthrough() -> None:
    transform = _probe_transform()

    assert transform.name == "aws_textract_inline_analysis"
    assert transform.determinism is Determinism.EXTERNAL_CALL
    assert transform.passes_through_input is True
    assert transform.input_schema is not None
    assert transform.output_schema is not None


def test_forward_invariant_probe_enriches_and_passes_through() -> None:
    transform = _probe_transform()
    rows = transform.forward_invariant_probe_rows(make_pipeline_row({"existing_field": "keep-me"}))
    assert len(rows) == 1

    result = transform.execute_forward_invariant_probe(rows, _probe_context())

    assert result.status == "success"
    output = result.row.to_dict()
    assert output["textract_text"] == "probe"
    assert output["existing_field"] == "keep-me"
    assert output["blob_ref"] == rows[0]["blob_ref"]


def test_forward_invariant_probe_success_reason_action() -> None:
    transform = _probe_transform()
    rows = transform.forward_invariant_probe_rows(make_pipeline_row({}))
    context = _probe_context()
    result = transform.execute_forward_invariant_probe(rows, context)

    assert result.success_reason["action"] == "enriched"
    assert [call["request_data"].to_dict()["operation"] for call in context.landscape.calls] == ["analyze_document"]


def test_forward_invariant_probe_uses_isolated_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    transform = _probe_transform()
    rows = transform.forward_invariant_probe_rows(make_pipeline_row({}))
    monkeypatch.setattr(
        "elspeth.plugins.transforms.aws.textract_inline_analysis.build_textract_sync_sdk_client",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("probe touched Textract network builder")),
    )

    result = transform.execute_forward_invariant_probe(rows, _probe_context())

    assert result.status == "success"

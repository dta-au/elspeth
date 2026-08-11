"""Opt-in live acceptance proof for asynchronous Amazon Textract analysis."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from elspeth.plugins.transforms.aws.textract_document_analysis import AWSTextractDocumentAnalysis
from elspeth.testing import make_pipeline_row

_RUN_GATE = "ELSPETH_RUN_LIVE_TEXTRACT"
_REQUIRED_INPUTS = (
    "ELSPETH_TEST_TEXTRACT_REGION",
    "ELSPETH_TEST_TEXTRACT_BUCKET",
    "ELSPETH_TEST_TEXTRACT_KEY",
    "ELSPETH_TEST_TEXTRACT_EXPECTED_TEXT",
)


@dataclass
class _LiveLandscapeRecorder:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def allocate_call_index(self, state_id: str) -> int:
        del state_id
        return len(self.calls)

    def record_call(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(call_id=f"live-call-{len(self.calls)}")


@pytest.mark.live_aws
@pytest.mark.live_provider
def test_async_textract_document_analysis_live() -> None:
    gate = os.getenv(_RUN_GATE)
    if gate is None:
        pytest.fail("live Amazon Textract proof gate is required", pytrace=False)
    if gate != "1":
        pytest.fail("live Amazon Textract proof gate is invalid", pytrace=False)

    values = {name: os.getenv(name) for name in _REQUIRED_INPUTS}
    if any(not value for value in values.values()):
        pytest.fail("live Amazon Textract proof inputs are incomplete", pytrace=False)

    region = values["ELSPETH_TEST_TEXTRACT_REGION"]
    bucket = values["ELSPETH_TEST_TEXTRACT_BUCKET"]
    key = values["ELSPETH_TEST_TEXTRACT_KEY"]
    expected_text = values["ELSPETH_TEST_TEXTRACT_EXPECTED_TEXT"]
    assert region is not None and bucket is not None and key is not None and expected_text is not None

    recorder = _LiveLandscapeRecorder()
    transform: AWSTextractDocumentAnalysis | None = None
    try:
        transform = AWSTextractDocumentAnalysis(
            {
                "region": region,
                "auth_mode": "default_chain",
                "bucket_field": "document_bucket",
                "key_field": "document_key",
                "feature_types": ["LAYOUT"],
                "text_field": "textract_text",
                "metadata_field": "textract_metadata",
                "poll_timeout_seconds": 3600.0,
                "batch_wait_timeout_seconds": 3690.0,
                "schema": {"mode": "observed"},
            }
        )
        transform.on_start(
            SimpleNamespace(
                landscape=recorder,
                node_id="live-textract-node",
                run_id="live-textract-run",
                telemetry_emit=lambda _event: None,
                rate_limit_registry=None,
                shutdown_event=None,
            )
        )
        result = transform._process_single_with_state(
            make_pipeline_row({"document_bucket": bucket, "document_key": key}),
            "live-textract-state",
            token_id="live-textract-token",
        )
    except Exception:
        pytest.fail("live Amazon Textract analysis failed", pytrace=False)
    finally:
        if transform is not None:
            try:
                transform.close()
            except Exception:
                pytest.fail("live Amazon Textract cleanup failed", pytrace=False)

    if result.status != "success" or result.row is None:
        pytest.fail("live Amazon Textract analysis did not succeed", pytrace=False)
    text = result.row["textract_text"]
    metadata = result.row["textract_metadata"]
    if type(text) is not str or expected_text not in text:
        pytest.fail("live Amazon Textract expected text was not found", pytrace=False)
    if not isinstance(metadata, Mapping):
        pytest.fail("live Amazon Textract metadata was unavailable", pytrace=False)
    page_count = metadata.get("page_count")
    block_count = metadata.get("block_count")
    if type(page_count) is not int or page_count <= 0 or type(block_count) is not int or block_count <= 0:
        pytest.fail("live Amazon Textract result counts were invalid", pytrace=False)
    operations = [call["request_data"].to_dict()["operation"] for call in recorder.calls]
    if operations.count("start_document_analysis") != 1 or operations.count("get_document_analysis") < 1:
        pytest.fail("live Amazon Textract audit calls were incomplete", pytrace=False)

"""Shared row-level guarantees for Azure safety transforms."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from elspeth.contracts import TransformResult
from elspeth.plugins.infrastructure.batching.ports import CollectorOutputPort
from elspeth.plugins.transforms.azure.content_safety import AzureContentSafety
from elspeth.plugins.transforms.azure.prompt_shield import AzurePromptShield
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context


@pytest.mark.parametrize("plugin_type", [AzureContentSafety, AzurePromptShield])
def test_all_fields_rejects_row_with_no_scannable_text(plugin_type: type[AzureContentSafety] | type[AzurePromptShield]) -> None:
    """A security control cannot validate a row without analyzing any field."""
    options: dict[str, Any] = {
        "endpoint": "https://test.cognitiveservices.azure.com",
        "api_key": "test-key",
        "fields": "all",
        "schema": {"mode": "observed"},
    }
    if plugin_type is AzureContentSafety:
        options["thresholds"] = {"hate": 2, "violence": 2, "sexual": 2, "self_harm": 2}

    transform = plugin_type(options)
    collector = CollectorOutputPort()
    ctx = make_context()
    transform.on_start(ctx)
    transform.connect_output(collector, max_pending=1)

    try:
        with patch.object(transform, "_get_http_client", side_effect=AssertionError("unexpected Azure request")) as get_client:
            transform.accept(make_pipeline_row({"id": 1, "count": 42}), ctx)
            transform.flush_batch_processing(timeout=10.0)
            get_client.assert_not_called()

        assert len(collector.results) == 1
        _, result, _ = collector.results[0]
        assert isinstance(result, TransformResult)
        assert result.status == "error"
        assert result.reason == {"reason": "no_scannable_fields"}
    finally:
        transform.close()

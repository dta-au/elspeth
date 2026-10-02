"""Required-control credit must agree with validated moderation thresholds."""

from __future__ import annotations

from dataclasses import replace

import pytest

from elspeth.contracts.plugin_capabilities import ControlRole, PluginCapability
from elspeth.plugins.infrastructure.preflight import plugin_preflight_mode
from elspeth.plugins.transforms.azure.content_safety import AzureContentSafety, AzureContentSafetyConfig
from elspeth.web.plugin_policy.coverage import control_coverage_findings
from tests.unit.web.plugin_policy.test_coverage import _llm, _safety, _state


@pytest.mark.parametrize("threshold", [6, 6.0, "6", "6.0"], ids=["integer", "float", "string", "decimal-string"])
def test_no_op_thresholds_never_receive_required_control_credit(threshold: object) -> None:
    options = {
        "endpoint": "https://test.cognitiveservices.azure.com",
        "api_key": "offline-placeholder",
        "fields": ["llm_response"],
        "thresholds": dict.fromkeys(("hate", "violence", "sexual", "self_harm"), threshold),
        "schema": {"mode": "observed"},
    }
    config = AzureContentSafetyConfig.from_dict(options, plugin_name="azure_content_safety")
    assert config.thresholds.hate == 6
    with plugin_preflight_mode(True):
        transform = AzureContentSafety(options)
        try:
            assert transform._check_thresholds(dict.fromkeys(("hate", "violence", "sexual", "self_harm"), 6)) is None
        finally:
            transform.close()
    node = replace(_safety("safety", "safe_in", "main"), options=options)
    state = _state(_llm(on_success="safe_in"), node)
    findings = control_coverage_findings(state, PluginCapability.CONTENT_SAFETY)
    assert len(findings) == 1
    assert findings[0].component_id == "judge"
    assert findings[0].reason == "output_not_post_dominated"


@pytest.mark.parametrize("threshold", [0, False, 5, 5.0, "5"])
def test_effective_thresholds_keep_blocking_credit(threshold: object) -> None:
    options = {"thresholds": {"hate": threshold, "violence": 6, "sexual": "6", "self_harm": 6.0}}
    assert AzureContentSafety.is_effective_blocking_control(
        capability=PluginCapability.CONTENT_SAFETY, role=ControlRole.OUTPUT, options=options
    )


@pytest.mark.parametrize("thresholds", [None, {}, {"hate": 5}, {"hate": "invalid", "violence": 6, "sexual": 6, "self_harm": 6}])
def test_unvalidated_thresholds_cannot_receive_blocking_credit(thresholds: object) -> None:
    assert not AzureContentSafety.is_effective_blocking_control(
        capability=PluginCapability.CONTENT_SAFETY, role=ControlRole.OUTPUT, options={"thresholds": thresholds}
    )

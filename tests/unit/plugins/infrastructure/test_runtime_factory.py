"""Direct unit tests for runtime_factory's raw-config Tier-3 boundary helpers.

These call ``validate_sink_effect_eligibility_from_raw_config`` and
``validate_landscape_export_settings_from_raw_config`` directly (not through
the CLI/execution-service callers that already exercise them end-to-end) so
the ``@trust_boundary`` honesty gate has a raising assertion that invokes the
decorated symbol through its declared ``source_param`` without one level of
indirection.
"""

import pytest

from elspeth.contracts.sink_effects import SinkEffectExecutionPurpose
from elspeth.plugins.infrastructure.runtime_factory import (
    _expand_env_placeholder_value,
    validate_landscape_export_settings_from_raw_config,
    validate_sink_effect_eligibility_from_raw_config,
)


def test_validate_sink_effect_eligibility_rejects_non_mapping_sinks():
    with pytest.raises(ValueError, match="'sinks' must be a mapping"):
        validate_sink_effect_eligibility_from_raw_config(
            {"sinks": ["not", "a", "mapping"]},
            purpose=SinkEffectExecutionPurpose.FRESH,
        )


def test_validate_landscape_export_settings_rejects_non_mapping_landscape():
    with pytest.raises(ValueError, match="'landscape' must be a mapping"):
        validate_landscape_export_settings_from_raw_config({"landscape": "not-a-mapping"})


def test_expand_env_placeholder_value_rejects_unset_variable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ELSPETH_B14_UNSET_PLACEHOLDER", raising=False)
    with pytest.raises(ValueError, match="Required environment variable 'ELSPETH_B14_UNSET_PLACEHOLDER' is not set"):
        _expand_env_placeholder_value(
            {"nested": ["${ELSPETH_B14_UNSET_PLACEHOLDER}"]},
            deferrable_env_vars=frozenset(),
        )


def test_expand_env_placeholder_value_defers_secret_loaded_variables(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ELSPETH_B14_STALE", "stale-process-value")
    expanded, fully_resolved = _expand_env_placeholder_value(
        {"url": "${ELSPETH_B14_STALE}", "count": 3, "tags": ["${ELSPETH_B14_MISSING:-fallback}"]},
        deferrable_env_vars=frozenset({"ELSPETH_B14_STALE"}),
    )
    assert expanded == {"url": "${ELSPETH_B14_STALE}", "count": 3, "tags": ["fallback"]}
    assert fully_resolved is False

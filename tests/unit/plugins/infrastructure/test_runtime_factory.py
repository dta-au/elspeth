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
from elspeth.core.config import load_settings_from_config_dict
from elspeth.plugins.infrastructure.runtime_factory import (
    _expand_env_placeholder_value,
    instantiate_plugins_from_config,
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


# elspeth-98a0a9e732 — an illegal node kind is reported BEFORE the plugin is
# constructed. ``llm`` is not batch-aware, so it is illegal as an aggregation
# and as a collector; these options also fail the llm constructor's own
# validation (no ``provider``), which is the message the author used to see
# instead of the kind error. The kind check reads ``is_batch_aware`` off the
# CLASS, so no instance is needed to answer it.
_ILLEGAL_LLM_OPTIONS = {"prompt_template": "Summarise {{ row.item }}", "schema": {"mode": "observed"}}

_BASE_DOC: dict[str, object] = {
    "sources": {
        "main": {
            "plugin": "csv",
            "on_success": "rows",
            "options": {"path": "in.csv", "on_validation_failure": "discard", "schema": {"mode": "observed"}},
        }
    },
    "sinks": {"out": {"plugin": "json", "options": {"path": "out.json", "schema": {"mode": "observed"}}, "on_write_failure": "discard"}},
    "transforms": [
        {
            "name": "explode",
            "plugin": "json_explode",
            "input": "rows",
            "on_success": "pages",
            "on_error": "discard",
            "options": {"array_field": "items", "schema": {"mode": "observed"}},
        },
    ],
}


def _settings_with_illegal_llm_aggregation():
    return load_settings_from_config_dict(
        {
            **_BASE_DOC,
            "aggregations": [
                {
                    "name": "digest",
                    "plugin": "llm",
                    "input": "pages",
                    "on_success": "out",
                    "on_error": "discard",
                    "trigger": {"count": 2},
                    "options": _ILLEGAL_LLM_OPTIONS,
                }
            ],
        }
    )


def _settings_with_illegal_llm_collector():
    return load_settings_from_config_dict(
        {
            **_BASE_DOC,
            "collectors": [{"name": "digest", "plugin": "llm", "input": "pages", "on_success": "out", "options": _ILLEGAL_LLM_OPTIONS}],
            "scopes": [{"name": "document", "opener": "explode", "closer": "digest", "policy": "require_all"}],
        }
    )


def test_illegal_aggregation_reports_the_node_kind_not_the_plugins_config_error():
    with pytest.raises(ValueError, match=r"Aggregation 'digest' uses transform 'llm' which has is_batch_aware=False") as excinfo:
        instantiate_plugins_from_config(_settings_with_illegal_llm_aggregation(), preflight_mode=True)
    assert "provider" not in str(excinfo.value)  # the llm constructor never ran


def test_illegal_collector_reports_the_node_kind_not_the_plugins_config_error():
    with pytest.raises(ValueError, match=r"Collector 'digest' uses transform 'llm' which has is_batch_aware=False") as excinfo:
        instantiate_plugins_from_config(_settings_with_illegal_llm_collector(), preflight_mode=True)
    assert "provider" not in str(excinfo.value)  # the llm constructor never ran

"""Regression tests for the 20261111-plugins-review tracing findings.

#124: azure_ai tracing configured process-global Azure Monitor BEFORE checking
the azure-ai-inference dependency, and silently accepted a later configuration
whose connection string or content-recording policy conflicted with the
active one.

#147: tracing privacy flags and credentials were not type-checked, so the
string "false" (what ``${VAR:-false}`` expands to) turned content recording ON.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest

from elspeth.plugins.transforms.llm.providers.azure import (
    _configure_azure_monitor,
    _reset_azure_monitor_state,
)
from elspeth.plugins.transforms.llm.tracing import AzureAITracingConfig, parse_tracing_config

_CONFIGURE_TARGET = "elspeth.plugins.transforms.llm.providers.azure.configure_azure_monitor"
_INSTRUMENTOR_MODULE = "azure.ai.inference.tracing"


@dataclass
class _RecordingAzureMonitor:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@dataclass
class _RecordingInstrumentor:
    calls: list[dict[str, Any]]

    def instrument(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def _instrumentor_module(calls: list[dict[str, Any]]) -> ModuleType:
    module = ModuleType(_INSTRUMENTOR_MODULE)
    module.AIInferenceInstrumentor = lambda: _RecordingInstrumentor(calls)
    return module


@pytest.fixture(autouse=True)
def _isolated_azure_monitor_state() -> Iterator[None]:
    _reset_azure_monitor_state()
    yield
    _reset_azure_monitor_state()


class TestAzureMonitorDependencyCheckedBeforeSideEffect:
    """#124: a missing instrumentor must not leave a process-global exporter live."""

    def test_missing_instrumentor_does_not_configure_azure_monitor(self) -> None:
        config = AzureAITracingConfig(connection_string="InstrumentationKey=k", enable_content_recording=False)
        azure_monitor = _RecordingAzureMonitor()

        # A None entry in sys.modules makes the import raise ImportError, which
        # is exactly a standard elspeth[azure] install without azure-ai-inference.
        with (
            patch(_CONFIGURE_TARGET, new=azure_monitor),
            patch.dict(sys.modules, {_INSTRUMENTOR_MODULE: None}),
            pytest.raises(ImportError, match="azure-ai-inference is not installed"),
        ):
            _configure_azure_monitor(config)

        assert azure_monitor.calls == [], "process-global Azure Monitor was configured before the dependency check"


class TestAzureMonitorConflictingReconfiguration:
    """#124: a second configuration must match the active one or be refused."""

    def _configure(self, config: AzureAITracingConfig, azure_monitor: _RecordingAzureMonitor, calls: list[dict[str, Any]]) -> bool:
        with (
            patch(_CONFIGURE_TARGET, new=azure_monitor),
            patch.dict(sys.modules, {_INSTRUMENTOR_MODULE: _instrumentor_module(calls)}),
        ):
            return _configure_azure_monitor(config)

    def test_identical_reconfiguration_is_idempotent_but_conflicting_policy_is_refused(self) -> None:
        azure_monitor = _RecordingAzureMonitor()
        instrument_calls: list[dict[str, Any]] = []
        first = AzureAITracingConfig(connection_string="InstrumentationKey=k", enable_content_recording=False)

        assert self._configure(first, azure_monitor, instrument_calls) is True
        assert self._configure(first, azure_monitor, instrument_calls) is True
        assert len(azure_monitor.calls) == 1
        assert instrument_calls == [{"enable_content_recording": False}]

        enabling = AzureAITracingConfig(connection_string="InstrumentationKey=k", enable_content_recording=True)
        with pytest.raises(ValueError, match="enable_content_recording"):
            self._configure(enabling, azure_monitor, instrument_calls)

        assert len(azure_monitor.calls) == 1
        assert instrument_calls == [{"enable_content_recording": False}]

    def test_conflicting_connection_string_is_refused(self) -> None:
        azure_monitor = _RecordingAzureMonitor()
        instrument_calls: list[dict[str, Any]] = []
        first = AzureAITracingConfig(connection_string="InstrumentationKey=first", enable_content_recording=False)
        second = AzureAITracingConfig(connection_string="InstrumentationKey=second", enable_content_recording=False)

        assert self._configure(first, azure_monitor, instrument_calls) is True
        with pytest.raises(ValueError, match="connection_string"):
            self._configure(second, azure_monitor, instrument_calls)

        assert len(azure_monitor.calls) == 1

    def test_conflicting_live_metrics_is_refused(self) -> None:
        azure_monitor = _RecordingAzureMonitor()
        instrument_calls: list[dict[str, Any]] = []
        first = AzureAITracingConfig(connection_string="InstrumentationKey=k", enable_live_metrics=False)
        second = AzureAITracingConfig(connection_string="InstrumentationKey=k", enable_live_metrics=True)

        assert self._configure(first, azure_monitor, instrument_calls) is True
        with pytest.raises(ValueError, match="enable_live_metrics"):
            self._configure(second, azure_monitor, instrument_calls)


_LANGFUSE_BASE: dict[str, Any] = {
    "provider": "langfuse",
    "public_key": "pk-lf-test",
    "secret_key": "sk-lf-test",
    "host": "https://langfuse.example.com",
}
_AZURE_BASE: dict[str, Any] = {"provider": "azure_ai", "connection_string": "InstrumentationKey=k"}


class TestTracingPolicyFlagsAreBool:
    """#147: a non-bool privacy/policy flag must be refused, never coerced by truthiness."""

    @pytest.mark.parametrize("field_name", ["enable_content_recording", "enable_live_metrics"])
    @pytest.mark.parametrize("value", ["false", "true", "", 0, 1, None])
    def test_azure_ai_rejects_non_bool_flag(self, field_name: str, value: object) -> None:
        with pytest.raises(ValueError, match=field_name):
            parse_tracing_config({**_AZURE_BASE, field_name: value})

    @pytest.mark.parametrize("value", ["false", "true", "", 0, 1, None])
    def test_langfuse_rejects_non_bool_tracing_enabled(self, value: object) -> None:
        with pytest.raises(ValueError, match="tracing_enabled"):
            parse_tracing_config({**_LANGFUSE_BASE, "tracing_enabled": value})


class TestTracingCredentialsAreNonEmptyStrings:
    """#147: empty or non-str credentials must be refused at construction."""

    @pytest.mark.parametrize("value", ["", "   ", 123, ["InstrumentationKey=k"]])
    def test_azure_ai_rejects_bad_connection_string(self, value: object) -> None:
        with pytest.raises(ValueError, match="connection_string"):
            parse_tracing_config({**_AZURE_BASE, "connection_string": value})

    @pytest.mark.parametrize("field_name", ["public_key", "secret_key"])
    @pytest.mark.parametrize("value", ["", "   ", 123, b"sk"])
    def test_langfuse_rejects_bad_key(self, field_name: str, value: object) -> None:
        with pytest.raises(ValueError, match=field_name):
            parse_tracing_config({**_LANGFUSE_BASE, field_name: value})

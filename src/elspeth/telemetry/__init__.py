"""Telemetry subsystem for pipeline observability.

This package provides Tier 1 (global) telemetry - framework-level audit events
streamed to external observability platforms. It complements but does not
replace the Landscape audit trail:

- Landscape: Legal record, complete lineage, persisted forever
- Telemetry: Operational visibility, real-time streaming, ephemeral

Components:
- events: Telemetry-specific event dataclasses (base TelemetryEvent in contracts)
- filtering: should_emit() for granularity-based event filtering
- manager: TelemetryManager for coordinating event emission
- protocols: ExporterProtocol for implementing exporters
- hookspecs: pluggy hooks for exporter discovery
- errors: TelemetryExporterError for configuration failures
- exporters: Built-in exporters (ConsoleExporter)

Usage:
    # Row-level events (TransformCompleted, GateEvaluated, TokenCompleted)
    # are in contracts as they cross the engine<->telemetry boundary:
    from elspeth.contracts import TransformCompleted, GateEvaluated, TokenCompleted

    # Telemetry-specific events:
    from elspeth.telemetry import (
        # Events
        RunStarted,
        RunFinished,
        PhaseChanged,
        FieldResolutionApplied,
        RowCreated,
        ExternalCallCompleted,
        # Manager
        TelemetryManager,
        should_emit,
        # Protocol
        ExporterProtocol,
        # Exporters
        ConsoleExporter,
        # Errors
        TelemetryExporterError,
    )
"""

from elspeth.contracts.events import (
    ExternalCallCompleted,
    FieldResolutionApplied,
    PhaseChanged,
    ResourceCleanupFailed,
    RowCreated,
    RunFinished,
    RunStarted,
)
from elspeth.telemetry.errors import TelemetryExporterError
from elspeth.telemetry.exporters import ConsoleExporter
from elspeth.telemetry.factory import create_telemetry_manager
from elspeth.telemetry.filtering import should_emit
from elspeth.telemetry.manager import TelemetryManager
from elspeth.telemetry.protocols import ExporterProtocol

__all__ = [
    "ConsoleExporter",
    "ExporterProtocol",
    "ExternalCallCompleted",
    "FieldResolutionApplied",
    "PhaseChanged",
    "ResourceCleanupFailed",
    "RowCreated",
    "RunFinished",
    "RunStarted",
    "TelemetryExporterError",
    "TelemetryManager",
    "create_telemetry_manager",
    "should_emit",
]

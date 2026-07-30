"""Protocol definitions for telemetry exporters.

Exporters are responsible for shipping telemetry events to external
observability platforms (OTLP, Azure Monitor, Datadog, etc.).
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, runtime_checkable

if TYPE_CHECKING:
    from elspeth.contracts.events import TelemetryEvent


class ExporterDeliveryMetrics(TypedDict):
    """Required exporter-native delivery accounting fields."""

    attempted: int
    delivered: int
    failed: int
    dropped: int
    pending: int
    consecutive_failures: int
    last_success_unix_nano: int | None
    lifecycle_failures: int


class DeliveryMetricsExporterProtocol(Protocol):
    """Optional capability for exporters with native delivery accounting.

    Deliberately NOT ``@runtime_checkable``. This is a documentation and
    static-typing contract for exporter authors, not an admission gate:
    ``isinstance()`` against it must raise ``TypeError`` rather than quietly
    become a duck-type check (ADR-032 rule 3). Exporters arrive from
    third-party pluggy plugins, and a runtime-checkable Protocol resolves
    members through ``inspect.getattr_static`` since Python 3.12 — it admits
    an impostor that merely declares the attribute names and rejects an
    honest exporter that forwards ``delivery_metrics`` through
    ``__getattr__``. ``TelemetryManager._exporter_delivery_metrics`` probes
    the capability with a sentinel-defaulted ``getattr`` and validates the
    returned VALUES instead.
    """

    @property
    def delivery_metrics(self) -> ExporterDeliveryMetrics:
        """Return a complete delivery-accounting snapshot."""
        ...


@runtime_checkable
class ExporterProtocol(Protocol):
    """Protocol for telemetry exporters.

    Exporters ship telemetry events to external observability platforms.
    They are discovered via pluggy hooks and configured via pipeline settings.

    Lifecycle:
        1. Discovery: elspeth_get_exporters hook returns exporter classes
        2. Instantiation: TelemetryManager creates instances
        3. Configuration: configure() called with exporter-specific settings
        4. Operation: export() called for each event (must not raise for handled transport failures)
        5. Shutdown: flush() then close() called at pipeline end

    Error handling:
        - configure() MUST raise TelemetryExporterError on invalid config
        - export() MUST NOT raise for handled transport failures - return False so TelemetryManager can account for them
        - close() MUST be idempotent - safe to call multiple times
    """

    @property
    def name(self) -> str:
        """Exporter name for configuration reference.

        This name is used in pipeline configuration to enable/configure
        the exporter:

            telemetry:
              exporters:
                - name: otlp  # matches this property
                  endpoint: http://localhost:4317
        """
        ...

    def configure(self, config: Mapping[str, Any]) -> None:
        """Configure the exporter with settings from pipeline configuration.

        Called once during TelemetryManager initialization, before any
        events are exported.

        Args:
            config: Exporter-specific configuration dict from pipeline settings

        Raises:
            TelemetryExporterError: If configuration is invalid or incomplete
        """
        ...

    def export(self, event: "TelemetryEvent") -> bool | None:
        """Export a single telemetry event.

        Called for each event emitted by the pipeline. Handled transport
        failures should be logged internally and reported by returning False;
        they should not raise and crash pipeline code. Programming errors may
        still raise so the telemetry manager can fail closed on corrupt code.

        Implementations may buffer events for batch export. Use flush() to
        ensure all buffered events are sent.

        Thread Safety:
            export() is always called from the telemetry export thread, never
            concurrently with itself. However, export() may run on a different
            thread than configure() and close(). Implementations should not
            rely on thread-local state from configure().

        Args:
            event: The telemetry event to export
        """
        ...

    def flush(self) -> bool | None:
        """Flush any buffered events to the destination.

        Called periodically and at pipeline shutdown to ensure events
        are delivered. Return False for a handled transport failure. Should be
        a no-op if no buffering is used.
        """
        ...

    def close(self) -> None:
        """Release any resources held by the exporter.

        Called at pipeline shutdown after flush(). Must be idempotent -
        calling close() multiple times should be safe.
        """
        ...

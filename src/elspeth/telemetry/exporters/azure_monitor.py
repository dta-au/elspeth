"""Azure Monitor exporter for telemetry events.

Exports telemetry events to Azure Monitor / Application Insights using
the azure-monitor-opentelemetry-exporter package.

Converts ELSPETH TelemetryEvents to OpenTelemetry Spans and ships them
to Application Insights for distributed tracing, monitoring, and alerting.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import structlog
from opentelemetry.sdk.trace.export import SpanExportResult

from elspeth.contracts.events import EngineSpanCompleted, EngineSpanName, EngineSpanStatus, RunFinished, RunStarted
from elspeth.telemetry.errors import TELEMETRY_TRANSPORT_ERRORS, TelemetryExporterError
from elspeth.telemetry.serialization import (
    SyntheticReadableSpan,
    derive_trace_id,
    generate_span_id,
    serialize_event_attributes,
)

if TYPE_CHECKING:
    from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter

    from elspeth.contracts.events import TelemetryEvent

logger = structlog.get_logger(__name__)

_MAX_TRACKED_TRACE_RUNS = 10_000


class AzureMonitorExporter:
    """Export telemetry events to Azure Monitor / Application Insights.

    Uses azure-monitor-opentelemetry-exporter for native integration with
    Azure observability stack. Spans appear in Application Insights under
    the "Distributed Tracing" blade.

    Configuration options:
        connection_string: Application Insights connection string (required).
            Typically from APPLICATIONINSIGHTS_CONNECTION_STRING env var.
        batch_size: Number of events to buffer before export (default: 100)
        service_name: Service name for resource attributes (default: "elspeth")
        service_version: Service version (optional)
        deployment_environment: Deployment environment (optional, e.g. "production")

    Example configuration:
        telemetry:
          exporters:
            - name: azure_monitor
              connection_string: ${APPLICATIONINSIGHTS_CONNECTION_STRING}
              batch_size: 100
              service_name: "my-pipeline"
              service_version: "1.0.0"
              deployment_environment: "production"

    Thread safety:
        Assumes single-threaded access. Buffer is not thread-safe.

    Azure-specific attributes:
        All spans include cloud.provider="azure" for filtering in
        Application Insights queries.

    Resource attributes:
        This exporter creates its own TracerProvider with proper Resource
        attributes. This avoids the ProxyTracerProvider issue where the
        Azure Monitor SDK tries to access `get_tracer_provider().resource`
        but gets a ProxyTracerProvider with no resource attribute.
    """

    _name = "azure_monitor"

    def __init__(self) -> None:
        """Initialize unconfigured exporter."""
        self._connection_string: str | None = None
        self._batch_size: int = 100
        self._service_name: str = "elspeth"
        self._service_version: str | None = None
        self._deployment_environment: str | None = None
        self._azure_exporter: AzureMonitorTraceExporter | None = None
        self._resource: Any | None = None  # Resource - stored for span creation
        self._buffer: list[TelemetryEvent] = []
        self._trace_started_at: dict[str, datetime] = {}
        self._fresh_run_ids: set[str] = set()
        self._configured: bool = False

    @property
    def name(self) -> str:
        """Exporter name for configuration reference."""
        return self._name

    def configure(self, config: Mapping[str, Any]) -> None:
        """Configure the exporter with settings from pipeline configuration.

        Args:
            config: Exporter-specific configuration dict containing:
                - connection_string (required): Application Insights connection string
                - batch_size (optional): Buffer size before auto-flush (default: 100)
                - service_name (optional): Service name for resource attributes (default: "elspeth")
                - service_version (optional): Service version for resource attributes
                - deployment_environment (optional): Deployment environment (e.g. "production")

        Raises:
            TelemetryExporterError: If connection_string is missing, wrong types provided,
                or Azure Monitor packages are not installed
        """
        if "connection_string" not in config:
            raise TelemetryExporterError(
                self._name,
                "Azure Monitor exporter requires 'connection_string' in config",
            )

        # Validate connection_string type
        connection_string = config["connection_string"]
        if not isinstance(connection_string, str):
            raise TelemetryExporterError(
                self._name,
                f"'connection_string' must be a string, got {type(connection_string).__name__}",
            )
        self._connection_string = connection_string

        # Validate batch_size type and value
        batch_size = config.get("batch_size", 100)
        if not isinstance(batch_size, int):
            raise TelemetryExporterError(
                self._name,
                f"'batch_size' must be an integer, got {type(batch_size).__name__}",
            )
        if batch_size < 1:
            raise TelemetryExporterError(
                self._name,
                f"batch_size must be >= 1, got {batch_size}",
            )
        self._batch_size = batch_size

        # Validate and extract service_name (required — distinguishes this
        # pipeline in Application Insights)
        if "service_name" not in config:
            raise TelemetryExporterError(
                self._name,
                "Azure Monitor exporter requires 'service_name' in config",
            )
        service_name = config["service_name"]
        if not isinstance(service_name, str):
            raise TelemetryExporterError(
                self._name,
                f"'service_name' must be a string, got {type(service_name).__name__}",
            )
        self._service_name = service_name

        # Validate and extract service_version (optional)
        service_version = config.get("service_version")
        if service_version is not None and not isinstance(service_version, str):
            raise TelemetryExporterError(
                self._name,
                f"'service_version' must be a string or null, got {type(service_version).__name__}",
            )
        self._service_version = service_version

        # Validate and extract deployment_environment (optional)
        deployment_environment = config.get("deployment_environment")
        if deployment_environment is not None and not isinstance(deployment_environment, str):
            raise TelemetryExporterError(
                self._name,
                f"'deployment_environment' must be a string or null, got {type(deployment_environment).__name__}",
            )
        self._deployment_environment = deployment_environment

        # Import and initialize the Azure Monitor exporter
        try:
            from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
            from opentelemetry.sdk.resources import SERVICE_NAME, Resource
            from opentelemetry.sdk.trace import TracerProvider

            # Build resource attributes for proper service identification in App Insights
            # This fixes the ProxyTracerProvider issue: without an explicit TracerProvider,
            # the Azure SDK calls get_tracer_provider() which returns a ProxyTracerProvider
            # that has no .resource attribute, causing AttributeError during export.
            resource_attributes: dict[str, str] = {
                SERVICE_NAME: self._service_name,
            }
            if self._service_version:
                resource_attributes["service.version"] = self._service_version
            if self._deployment_environment:
                resource_attributes["deployment.environment"] = self._deployment_environment

            # Create and store resource for both TracerProvider and span creation
            self._resource = Resource.create(resource_attributes)
            tracer_provider = TracerProvider(resource=self._resource)

            # Pass our TracerProvider to the exporter so it doesn't fall back to
            # get_tracer_provider() which would return ProxyTracerProvider
            self._azure_exporter = AzureMonitorTraceExporter(
                connection_string=self._connection_string,
                tracer_provider=tracer_provider,
            )
        except ImportError as e:
            raise TelemetryExporterError(
                self._name,
                f"Azure Monitor exporter not installed: {e}. Install with: uv pip install azure-monitor-opentelemetry-exporter",
            ) from e

        self._configured = True
        self._buffer = []
        self._reset_trace_registry()

        logger.debug(
            "Azure Monitor exporter configured",
            batch_size=self._batch_size,
            service_name=self._service_name,
            service_version=self._service_version,
            deployment_environment=self._deployment_environment,
        )

    def export(self, event: TelemetryEvent) -> bool | None:
        """Export a single telemetry event.

        Events are buffered until batch_size is reached, then flushed.
        Handled transport failures return False so TelemetryManager can account
        for them without crashing the pipeline.

        Args:
            event: The telemetry event to export
        """
        if not self._configured:
            logger.warning(
                "Azure Monitor exporter not configured, dropping event",
                event_type=type(event).__name__,
            )
            return False

        try:
            self._buffer.append(event)
            if len(self._buffer) >= self._batch_size:
                return self._flush_batch()
        except Exception as e:
            if not isinstance(e, TELEMETRY_TRANSPORT_ERRORS):
                raise  # Programming error — must crash
            logger.warning(
                "Failed to buffer telemetry event",
                exporter=self._name,
                event_type=type(event).__name__,
                error=str(e),
            )
            return False
        return None

    def _flush_batch(self) -> bool | None:
        """Convert buffered events to spans and export to Azure Monitor.

        Called internally when buffer reaches batch_size, and
        externally via flush(). Returns False when a handled transport failure
        prevents delivery.
        """
        if not self._buffer:
            logger.debug(
                "Azure Monitor flush requested with empty buffer",
                exporter=self._name,
            )
            return None

        if not self._azure_exporter:
            logger.warning("Azure Monitor exporter not initialized, dropping batch")
            self._buffer.clear()
            return False

        try:
            spans = [self._event_to_span(e) for e in self._buffer]
            result = self._azure_exporter.export(spans)
            if result == SpanExportResult.FAILURE:
                logger.warning(
                    "Azure Monitor exporter reported failed status",
                    exporter=self._name,
                    span_count=len(spans),
                )
                return False
            logger.debug(
                "Azure Monitor batch exported",
                span_count=len(spans),
            )
        except Exception as e:
            if not isinstance(e, TELEMETRY_TRANSPORT_ERRORS):
                raise  # Programming error — must crash
            logger.warning(
                "Failed to export Azure Monitor batch",
                exporter=self._name,
                span_count=len(self._buffer),
                error=str(e),
            )
            return False
        finally:
            self._buffer.clear()
        return None

    def _event_to_span(self, event: TelemetryEvent) -> SyntheticReadableSpan:
        """Convert TelemetryEvent to OpenTelemetry ReadableSpan.

        Reuses SyntheticReadableSpan from the shared serialization module.
        Adds Azure-specific attributes for better filtering in Application Insights.

        Args:
            event: The telemetry event to convert

        Returns:
            ReadableSpan-compatible object suitable for Azure Monitor export
        """
        from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags

        event_type = type(event)
        if event_type is EngineSpanCompleted:
            engine_event = cast("EngineSpanCompleted", event)
            self._bind_trace_started_at(engine_event.run_id, engine_event.trace_started_at)
            trace_id = derive_trace_id(engine_event.run_id, started_at=engine_event.trace_started_at)
            span_id = int(engine_event.span_id, 16)
            trace_flags = TraceFlags(TraceFlags.SAMPLED)
            parent = None
            if engine_event.parent_span_id is not None:
                parent = SpanContext(
                    trace_id=trace_id,
                    span_id=int(engine_event.parent_span_id, 16),
                    is_remote=False,
                    trace_flags=trace_flags,
                )
            attributes: dict[str, Any] = dict(engine_event.attributes)
            attributes["run_id"] = engine_event.run_id
            attributes["event_type"] = type(engine_event).__name__
            if engine_event.exception_type is not None:
                attributes["exception_type"] = engine_event.exception_type
            attributes["cloud.provider"] = "azure"
            attributes["elspeth.exporter"] = "azure_monitor"
            status = Status(StatusCode.ERROR if engine_event.status is EngineSpanStatus.ERROR else StatusCode.OK)
            span = SyntheticReadableSpan(
                name=engine_event.name.value,
                context=SpanContext(
                    trace_id=trace_id,
                    span_id=span_id,
                    is_remote=False,
                    trace_flags=trace_flags,
                ),
                parent=parent,
                attributes=attributes,
                start_time=int(engine_event.started_at.timestamp() * 1_000_000_000),
                end_time=int(engine_event.timestamp.timestamp() * 1_000_000_000),
                kind=SpanKind.INTERNAL,
                resource=self._resource,
                status=status,
            )
            if engine_event.name is EngineSpanName.RUN:
                self._record_run_span_completed(engine_event.run_id)
            return span

        # Preserve the durable RunStarted trace origin for all lifecycle events
        # so point telemetry and completed engine spans remain in one trace.
        if event_type is RunStarted:
            run_started = cast("RunStarted", event)
            self._bind_trace_started_at(run_started.run_id, run_started.timestamp)
            self._fresh_run_ids.add(run_started.run_id)
        elif event.run_id not in self._trace_started_at:
            self._bind_trace_started_at(event.run_id, event.timestamp)
        started_at = self._trace_started_at[event.run_id]
        trace_id = derive_trace_id(event.run_id, started_at=started_at)
        span_id = generate_span_id()

        # Convert timestamp to nanoseconds since epoch
        if event.timestamp.tzinfo is None:
            ts = event.timestamp.replace(tzinfo=UTC)
        else:
            ts = event.timestamp
        timestamp_ns = int(ts.timestamp() * 1_000_000_000)

        # Build attributes from event fields with Azure-specific additions
        attributes = self._serialize_event_attributes(event)

        # Add Azure-specific attributes for filtering in Application Insights
        attributes["cloud.provider"] = "azure"
        attributes["elspeth.exporter"] = "azure_monitor"

        # Create span context
        span_context = SpanContext(
            trace_id=trace_id,
            span_id=span_id,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )

        # Create a ReadableSpan with resource attributes for proper service identification
        span = SyntheticReadableSpan(
            name=type(event).__name__,
            context=span_context,
            attributes=attributes,
            start_time=timestamp_ns,
            end_time=timestamp_ns,  # Instant span
            kind=SpanKind.INTERNAL,
            resource=self._resource,  # Pass resource for service.name, etc.
        )

        if event_type is RunFinished:
            run_finished = cast("RunFinished", event)
            self._record_run_finished(run_finished.run_id)

        return span

    def _bind_trace_started_at(self, run_id: str, started_at: datetime) -> None:
        """Bind or verify one run's durable trace origin."""
        normalized = started_at.replace(tzinfo=UTC) if started_at.tzinfo is None else started_at.astimezone(UTC)
        if run_id in self._trace_started_at:
            existing = self._trace_started_at[run_id]
            if existing != normalized:
                raise TelemetryExporterError(self._name, "run trace start changed after binding")
            return
        if len(self._trace_started_at) >= _MAX_TRACKED_TRACE_RUNS:
            raise TelemetryExporterError(self._name, "active trace identity capacity exceeded")
        self._trace_started_at[run_id] = normalized

    def _record_run_finished(self, run_id: str) -> None:
        """Release trace state after the terminal point event is converted."""
        self._clear_trace_state(run_id)

    def _record_run_span_completed(self, run_id: str) -> None:
        """Release standalone/late run spans; fresh runs await RunFinished."""
        if run_id not in self._fresh_run_ids:
            self._clear_trace_state(run_id)

    def _clear_trace_state(self, run_id: str) -> None:
        """Remove every registry entry for one terminal run."""
        if run_id in self._trace_started_at:
            del self._trace_started_at[run_id]
        if run_id in self._fresh_run_ids:
            self._fresh_run_ids.remove(run_id)

    def _reset_trace_registry(self) -> None:
        """Discard exporter-local correlation state at a lifecycle boundary."""
        self._trace_started_at.clear()
        self._fresh_run_ids.clear()

    @staticmethod
    def _serialize_event_attributes(event: TelemetryEvent) -> dict[str, Any]:
        """Serialize event fields as span attributes."""
        return serialize_event_attributes(event)

    def flush(self) -> bool | None:
        """Flush any buffered events to Azure Monitor.

        Called periodically and at pipeline shutdown to ensure events
        are delivered. Returns False for a handled transport failure.
        """
        try:
            return self._flush_batch()
        except Exception as e:
            if not isinstance(e, TELEMETRY_TRANSPORT_ERRORS):
                raise  # Programming error — must crash
            logger.warning(
                "Failed to flush Azure Monitor exporter",
                exporter=self._name,
                error=str(e),
            )
            return False

    def close(self) -> None:
        """Release resources held by the exporter.

        Flushes any remaining buffered events and shuts down the
        underlying Azure Monitor exporter. Idempotent - safe to call
        multiple times.
        """
        self.flush()
        if self._azure_exporter:
            try:
                self._azure_exporter.shutdown()
            except Exception as e:
                if not isinstance(e, TELEMETRY_TRANSPORT_ERRORS):
                    raise  # Programming error — must crash
                logger.warning(
                    "Failed to shutdown Azure Monitor exporter",
                    exporter=self._name,
                    error=str(e),
                )
            self._azure_exporter = None
        self._reset_trace_registry()
        self._configured = False

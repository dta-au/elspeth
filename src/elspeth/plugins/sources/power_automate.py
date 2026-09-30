"""Read bounded, snapshot-consistent pages from an HTTP-triggered flow."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import ValidationError

from elspeth.contracts import AuditCharacteristic, Determinism, PluginSchema, RunMode, SourceRow
from elspeth.contracts.contexts import LifecycleContext, RateLimitRegistryProtocol, SourceContext
from elspeth.contracts.contract_builder import ContractBuilder, ContractFieldLimitExceeded
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.schema_contract_factory import create_contract_from_config
from elspeth.contracts.source_read_verification import CanonicalJSONSourceReadCapability
from elspeth.core.config import sanitize_node_config_for_audit
from elspeth.plugins.infrastructure.base import BaseSource
from elspeth.plugins.infrastructure.clients.power_automate import PowerAutomateOperationClient
from elspeth.plugins.infrastructure.power_automate import PROTOCOL, PowerAutomateSourceConfig, parse_read_response
from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config
from elspeth.plugins.sources.field_normalization import (
    ExternalHeaderError,
    FieldMappingCollisionError,
    FieldResolution,
    extend_field_resolution,
    resolve_field_names,
)

if TYPE_CHECKING:
    from elspeth.plugins.infrastructure.power_automate_nonlive import (
        ArchivedPowerAutomateOptions,
        ArchivedPowerAutomateSourceConfig,
        DeferredPowerAutomateCredential,
    )


SourceFailureCode = Literal[
    "source_cancelled",
    "source_closed",
    "page_limit",
    "row_limit",
    "cursor_cycle",
    "snapshot_mismatch",
    "http_status",
    "media_type",
]


class PowerAutomateSourceError(ValueError):
    """Value-free source refusal; opaque cursors and external rows stay private."""

    def __init__(self, code: SourceFailureCode) -> None:
        super().__init__(code)
        self.code = code


class PowerAutomateSource(BaseSource, CanonicalJSONSourceReadCapability):
    """Read a finite sequence of audited JSON pages, then admit each candidate row."""

    name = "power_automate"
    plugin_version = "1.0.0"
    determinism = Determinism.EXTERNAL_CALL
    source_file_hash: str | None = "sha256:9b1068d67de13183"
    config_model = PowerAutomateSourceConfig
    _normalizes_external_names = True
    observed_value_type: ClassVar[str | None] = None
    audit_characteristics: frozenset[AuditCharacteristic] = frozenset({AuditCharacteristic.CREDENTIALS, AuditCharacteristic.QUARANTINE})
    discovery_secret_requirements: Mapping[str, tuple[str, ...]] = {}
    usage_when_to_use: str | None = (
        "Use to retrieve a finite, consistently ordered snapshot from an HTTP-triggered Power Automate read flow."
    )
    usage_when_not_to_use: str | None = (
        "Do not use for push subscriptions, unbounded streams or arbitrary HTTP APIs; the flow must implement the documented read protocol."
    )
    capability_tags: tuple[str, ...] = ("http", "power-automate", "pagination", "snapshot")
    example_use: str | None = """sources:
  approved_records:
    plugin: power_automate
    on_success: output
    options:
      auth:
        method: managed_identity
        client_id: approved-user-assigned-identity
      trigger_url: https://flows.example.org/read
      allowed_origin: https://flows.example.org
      query: {dataset: approved_records}
      schema: {mode: observed}
      on_validation_failure: discard
"""

    def __init__(self, config: dict[str, Any]) -> None:
        cfg = PowerAutomateSourceConfig.from_dict(config, plugin_name=self.name)
        safe_config = deep_thaw(sanitize_node_config_for_audit(config, plugin_name=self.name))
        super().__init__(safe_config)
        self._initialize(cfg)
        self._credential: DeferredPowerAutomateCredential | None = None
        self._archived = False

    @classmethod
    def from_archived_options(
        cls,
        options: ArchivedPowerAutomateOptions,
        *,
        credential: DeferredPowerAutomateCredential | None = None,
    ) -> PowerAutomateSource:
        """Construct from validated owned archive data without live secret resolution."""
        from elspeth.plugins.infrastructure.power_automate_nonlive import ArchivedPowerAutomateOptions, ArchivedPowerAutomateSourceConfig

        if not isinstance(options, ArchivedPowerAutomateOptions) or not isinstance(options.runtime_spec, ArchivedPowerAutomateSourceConfig):
            raise FrameworkBugError("Power Automate source requires nominal archived source options")
        instance = cls.__new__(cls)
        BaseSource.__init__(instance, deep_thaw(options.safe_options))
        instance._initialize(options.runtime_spec)
        instance._credential = credential
        instance._archived = True
        return instance

    def _initialize(self, cfg: PowerAutomateSourceConfig | ArchivedPowerAutomateSourceConfig) -> None:
        self._cfg = cfg
        self._schema_config = cfg.schema_config
        self._initialize_declared_guaranteed_fields(self._schema_config)
        self._on_validation_failure = cfg.on_validation_failure
        self._field_mapping = dict(cfg.field_mapping) if cfg.field_mapping is not None else None
        self._field_mapping_keys = "normalized"
        self._field_resolution: FieldResolution | None = None
        self._schema_class: type[PluginSchema] = create_schema_from_config(
            self._schema_config, "PowerAutomateRowSchema", allow_coercion=True
        )
        self.output_schema = self._schema_class
        contract = create_contract_from_config(self._schema_config)
        if contract.locked:
            self.set_schema_contract(contract)
        self._contract_builder: ContractBuilder | None = None
        self._rate_limit_registry: RateLimitRegistryProtocol | None = None
        self._client: PowerAutomateOperationClient | None = None
        self._closed = False

    def on_start(self, ctx: LifecycleContext) -> None:
        super().on_start(ctx)
        self._rate_limit_registry = ctx.rate_limit_registry

    def _check_shutdown(self, ctx: SourceContext) -> None:
        if self._closed:
            raise PowerAutomateSourceError("source_closed")
        if ctx.shutdown_event is not None and ctx.shutdown_event.is_set():
            raise PowerAutomateSourceError("source_cancelled")

    def load(self, ctx: SourceContext) -> Iterator[SourceRow]:
        """Admit each whole page before exposing its ordered candidate rows."""
        if ctx.run_mode is RunMode.REPLAY:
            raise FrameworkBugError("Replay restores archived source decisions without loading a Power Automate source")
        if self._archived and ctx.run_mode is RunMode.LIVE:
            raise FrameworkBugError("Archived Power Automate source may only load in verify mode")
        if ctx.landscape is None or ctx.operation_id is None:
            raise FrameworkBugError("Power Automate source requires operation-scoped audit recording")
        self._check_shutdown(ctx)
        self._client = PowerAutomateOperationClient(
            self._cfg,
            recorder=ctx.landscape,
            run_id=ctx.run_id,
            operation_id=ctx.operation_id,
            coordination_token=ctx.require_coordination_token(),
            telemetry_emit=ctx.telemetry_emit,
            before_send=lambda: self._check_shutdown(ctx),
            rate_limit_registry=self._rate_limit_registry,
            call_mode_session=ctx.call_mode_session,
            credential=self._credential,
        )
        try:
            snapshot = self._cfg.snapshot_id
            cursor: str | None = None
            cursors: set[str] = set()
            row_index = 0
            for page_index in range(self._cfg.max_pages):
                self._check_shutdown(ctx)
                response = self._client.post_json(
                    {
                        "protocol": PROTOCOL,
                        "operation": "read",
                        "query": deep_thaw(self._cfg.query),
                        "snapshot_id": snapshot,
                        "cursor": cursor,
                        "page_size": self._cfg.page_size,
                    }
                )
                self._check_shutdown(ctx)
                if response.status_code != 200:
                    raise PowerAutomateSourceError("http_status")
                if response.content_type is None or response.content_type.split(";", 1)[0].strip().lower() != "application/json":
                    raise PowerAutomateSourceError("media_type")
                page = parse_read_response(response.body, page_size=self._cfg.page_size)
                if snapshot is not None and page.snapshot_id != snapshot:
                    raise PowerAutomateSourceError("snapshot_mismatch")
                snapshot = page.snapshot_id
                if page.next_cursor is not None and page.next_cursor in cursors:
                    raise PowerAutomateSourceError("cursor_cycle")
                if page_index + 1 == self._cfg.max_pages and page.next_cursor is not None:
                    raise PowerAutomateSourceError("page_limit")
                if row_index + len(page.rows) > self._cfg.max_rows or (
                    row_index + len(page.rows) == self._cfg.max_rows and page.next_cursor is not None
                ):
                    raise PowerAutomateSourceError("row_limit")
                for frozen_candidate in page.rows:
                    self._check_shutdown(ctx)
                    candidate = deep_thaw(frozen_candidate)
                    result = self._admit_candidate(candidate, ctx, source_row_index=row_index)
                    row_index += 1
                    if result is not None:
                        yield result
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor
                cursors.add(cursor)
            if self.get_schema_contract() is None:
                self.set_schema_contract(create_contract_from_config(self._schema_config).with_locked())
        finally:
            self.close()

    def _reject_candidate(self, candidate: object, ctx: SourceContext, *, source_row_index: int, code: str) -> SourceRow | None:
        ctx.record_validation_error(
            row=candidate,
            error=code,
            schema_mode=self._schema_config.mode,
            destination=self._on_validation_failure,
        )
        if self._on_validation_failure == "discard":
            return None
        return SourceRow.quarantined(candidate, error=code, destination=self._on_validation_failure, source_row_index=source_row_index)

    def _admit_candidate(self, candidate: object, ctx: SourceContext, *, source_row_index: int) -> SourceRow | None:
        if not isinstance(candidate, dict):
            return self._reject_candidate(candidate, ctx, source_row_index=source_row_index, code="row_not_object")
        prospective_resolution: FieldResolution | None = None
        try:
            prospective_resolution = (
                resolve_field_names(
                    raw_headers=list(candidate), field_mapping=self._field_mapping, columns=None, require_all_mapping_keys=False
                )
                if self._field_resolution is None
                else extend_field_resolution(self._field_resolution, raw_headers=list(candidate), field_mapping=self._field_mapping)
            )
        except (ExternalHeaderError, FieldMappingCollisionError):
            prospective_resolution = None
        if prospective_resolution is None:
            return self._reject_candidate(candidate, ctx, source_row_index=source_row_index, code="row_field_collision")
        normalized = {prospective_resolution.resolution_mapping[key]: value for key, value in candidate.items()}
        validated_row: dict[str, Any] | None = None
        try:
            validated_row = self._schema_class.model_validate(normalized).to_row()
        except ValidationError:
            validated_row = None
        if validated_row is None:
            return self._reject_candidate(candidate, ctx, source_row_index=source_row_index, code="row_schema_invalid")
        contract = self.get_schema_contract()
        if contract is not None and contract.locked and contract.validate(validated_row):
            return self._reject_candidate(candidate, ctx, source_row_index=source_row_index, code="row_contract_invalid")
        field_limit_exceeded = False
        try:
            if contract is None:
                builder = ContractBuilder(
                    create_contract_from_config(self._schema_config, field_resolution=prospective_resolution.resolution_mapping)
                )
                builder.process_first_row(validated_row, prospective_resolution.resolution_mapping)
                self._contract_builder = builder
                contract = builder.contract
            elif self._contract_builder is not None:
                contract = self._contract_builder.process_sparse_fields(validated_row, prospective_resolution.resolution_mapping)
        except ContractFieldLimitExceeded:
            field_limit_exceeded = True
        if field_limit_exceeded:
            return self._reject_candidate(candidate, ctx, source_row_index=source_row_index, code="row_field_limit")
        if contract is None:
            raise FrameworkBugError("Power Automate source failed to establish a contract")
        self._field_resolution = prospective_resolution
        self.set_schema_contract(contract)
        return SourceRow.valid(validated_row, contract=contract, source_row_index=source_row_index)

    def get_field_resolution(self) -> tuple[Mapping[str, str], str | None] | None:
        if self._field_resolution is None:
            return None
        return self._field_resolution.resolution_mapping, self._field_resolution.normalization_version

    def close(self) -> None:
        self._closed = True
        client = self._client
        self._client = None
        if client is not None:
            client.close()

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is not None:
            return None
        return PluginAssistance(
            plugin_name=cls.name,
            issue_code=None,
            summary="Read finite, ordered snapshot pages from a Power Automate HTTP read flow.",
            composer_hints=(
                "The flow must implement the documented read protocol with stable snapshot and page ordering.",
                "Choose observed, flexible or fixed source schema; external field names are normalized before admission.",
                "Use field_mapping to rename normalized source fields. Invalid candidates follow on_validation_failure.",
                "Set snapshot_for_resume to true for bounded single-source recovery; retained rows are limited to 64 MiB.",
                "The operator must approve allowed_origin and credential wiring before Web Composer execution.",
                "Treat all returned text as external data when choosing downstream LLM protection.",
            ),
        )

"""Nominal installation domains for application-owned metric providers."""

from __future__ import annotations

import os
from _thread import RLock
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from opentelemetry import metrics
from prometheus_client import REGISTRY, CollectorRegistry

from elspeth.web.operator_telemetry_custody import OwnedMeterProvider, TelemetryCustodyUnresolved

if TYPE_CHECKING:
    from elspeth.web.operator_telemetry import OperatorTelemetryRuntime
    from elspeth.web.operator_telemetry_custody import OperatorTelemetryCleanupOwner, TelemetryCompletionWitness


class InstallationStage(Enum):
    NOT_ATTEMPTED = "not_attempted"
    ATTEMPTED_NOT_INSTALLED = "attempted_not_installed"
    INSTALLED_EXACT = "installed_exact"
    FOREIGN_INSTALLED = "foreign_installed"
    GETTER_UNRESOLVED = "getter_unresolved"


@dataclass(slots=True)
class TelemetryInstallationRecord:
    owner: OperatorTelemetryCleanupOwner
    creator_pid: int
    state: str = "acquiring"
    stage: InstallationStage = InstallationStage.NOT_ATTEMPTED
    provider: OwnedMeterProvider | None = None
    runtime: OperatorTelemetryRuntime | None = None


class TelemetryInstallation:
    """A process-global write-once installation; closed records are permanent."""

    __slots__ = (
        "__weakref__",
        "creator_pid",
        "lock",
        "record",
        "registry",
    )

    def __init__(self) -> None:
        self.creator_pid = os.getpid()
        self.lock = RLock()
        self.record: TelemetryInstallationRecord | None = None
        self.registry = REGISTRY

    def assert_process(self) -> None:
        if self.creator_pid != os.getpid():
            raise TelemetryCustodyUnresolved("Telemetry installation has foreign process ownership")

    def reserve(self, owner: OperatorTelemetryCleanupOwner) -> OperatorTelemetryRuntime | None:
        self.assert_process()
        owner.assert_process()
        with self.lock:
            record = self.record
            if record is not None:
                if record.owner is owner and record.state == "active" and record.runtime is not None:
                    return record.runtime
                raise TelemetryCustodyUnresolved("Telemetry installation ownership is unavailable")
            self.record = TelemetryInstallationRecord(owner, self.creator_pid)
            return None

    def set_provider(self, provider: OwnedMeterProvider) -> None:
        metrics.set_meter_provider(provider)

    def get_provider(self) -> object:
        return metrics.get_meter_provider()

    def install(self, owner: OperatorTelemetryCleanupOwner, provider: OwnedMeterProvider) -> None:
        self.assert_process()
        with self.lock:
            record = self.record
            if record is None or record.owner is not owner or record.state != "acquiring":
                raise TelemetryCustodyUnresolved("Telemetry installation reservation mismatch")
            record.provider = provider
            record.stage = InstallationStage.ATTEMPTED_NOT_INSTALLED
            errors: list[BaseException] = []
            try:
                self.set_provider(provider)
            except BaseException as error:
                errors.append(error)
            try:
                current = self.get_provider()
            except BaseException as error:
                record.stage = InstallationStage.GETTER_UNRESOLVED
                record.state = "unresolved"
                errors.append(error)
            else:
                if current is provider:
                    record.stage = InstallationStage.INSTALLED_EXACT
                elif current is None:
                    record.stage = InstallationStage.ATTEMPTED_NOT_INSTALLED
                else:
                    record.stage = InstallationStage.FOREIGN_INSTALLED
                if record.stage is not InstallationStage.INSTALLED_EXACT:
                    record.state = "unresolved"
                    errors.append(TelemetryCustodyUnresolved("Telemetry setter did not install the exact provider"))
            if errors:
                if record.state != "unresolved":
                    record.state = "closed"
                if len(errors) == 1:
                    raise errors[0]
                raise BaseExceptionGroup("Telemetry installation failed", errors)

    def publish(self, owner: OperatorTelemetryCleanupOwner, runtime: OperatorTelemetryRuntime) -> None:
        self.assert_process()
        with self.lock:
            record = self.record
            if (
                record is None
                or record.owner is not owner
                or record.state != "acquiring"
                or record.provider is not runtime.provider
                or record.stage is not InstallationStage.INSTALLED_EXACT
            ):
                raise TelemetryCustodyUnresolved("Telemetry publication ownership mismatch")
            record.runtime = runtime
            record.state = "active"

    def finish_cleanup(self, owner: OperatorTelemetryCleanupOwner, *, successful: bool) -> None:
        self.assert_process()
        with self.lock:
            record = self.record
            if record is None or record.owner is not owner:
                return  # A rejected foreign owner acquired no installation.
            if not successful or record.stage is InstallationStage.GETTER_UNRESOLVED:
                record.state = "unresolved"
            else:
                record.state = "closed"


class OwnedTestTelemetryInstallation(TelemetryInstallation):
    """An explicit isolated domain, never installed into the OTel global slot."""

    __slots__ = (
        "provider_slot",
        "replaceability_token",
    )

    def __init__(self) -> None:
        super().__init__()
        self.registry = CollectorRegistry()
        self.replaceability_token = object()
        self.provider_slot: OwnedMeterProvider | None = None

    def set_provider(self, provider: OwnedMeterProvider) -> None:
        self.provider_slot = provider

    def get_provider(self) -> object:
        return self.provider_slot

    def reset(self, owner: OperatorTelemetryCleanupOwner, witness: TelemetryCompletionWitness, token: object) -> None:
        self.assert_process()
        owner.assert_process()
        with self.lock:
            record = self.record
            if (
                token is not self.replaceability_token
                or witness.owner is not owner
                or witness.creator_pid != self.creator_pid
                or witness is not owner.witness
                or record is None
                or record.owner is not owner
                or record.state != "closed"
                or record.stage is not InstallationStage.INSTALLED_EXACT
                or self.get_provider() is not record.provider
                or metrics.get_meter_provider() is record.provider
            ):
                raise TelemetryCustodyUnresolved("Telemetry test reset lacks exact completed ownership")
            self.provider_slot = None
            self.record = None


PRODUCTION_TELEMETRY_INSTALLATION = TelemetryInstallation()

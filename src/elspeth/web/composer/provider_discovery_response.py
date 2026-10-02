"""Policy-owned discovery response envelopes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from pydantic import JsonValue

from elspeth.contracts.errors import FrameworkBugError
from elspeth.web.composer.response_contracts import AdmittedResponse, ResponseContract
from elspeth.web.composer.state import Severity, ValidationEntry
from elspeth.web.composer.tools._common import ToolResult

if TYPE_CHECKING:
    from elspeth.web.composer.planner_authoring_aids import PlannerPluginContract
    from elspeth.web.composer.protocol import ToolArgumentError


class _UncachedProjection(AdmittedResponse):
    def readmit(self, contract: ResponseContract) -> AdmittedResponse:
        raise FrameworkBugError("Policy-owned provider projections cannot be cached as producer responses")


@dataclass(frozen=True, slots=True)
class _ProjectedPluginContractResponse(_UncachedProjection):
    contract: PlannerPluginContract

    def to_wire(self) -> dict[str, JsonValue]:
        # The owned bounded projection has already admitted its schema leaves.
        # This cast narrows serialization output, never an admitted input root.
        return cast(dict[str, JsonValue], self.contract.to_dict())


def projected_plugin_contract_response(contract: PlannerPluginContract) -> AdmittedResponse:
    return _ProjectedPluginContractResponse(contract)


@dataclass(frozen=True, slots=True)
class _ArgumentErrorResponse(_UncachedProjection):
    component: str
    error_code: str

    def to_wire(self) -> dict[str, JsonValue]:
        return {
            "argument_error": {
                "component": self.component,
                "severity": "high",
                "error_code": self.error_code,
                "error_class": "ToolArgumentError",
            }
        }


def argument_error_response(error: ToolArgumentError) -> AdmittedResponse:
    return _ArgumentErrorResponse(error.argument, error.code or "argument_error")


@dataclass(frozen=True, slots=True)
class ClosedProviderValidationEntry:
    severity: Severity
    error_code: str

    def to_wire(self) -> dict[str, JsonValue]:
        return {"component": "pipeline", "severity": self.severity, "error_code": self.error_code}


@dataclass(frozen=True, slots=True)
class ClosedProviderValidation:
    is_valid: bool
    errors: tuple[ClosedProviderValidationEntry, ...]
    warnings: tuple[ClosedProviderValidationEntry, ...]
    suggestions: tuple[ClosedProviderValidationEntry, ...]

    def to_wire(self) -> dict[str, JsonValue]:
        return {
            "is_valid": self.is_valid,
            "errors": [entry.to_wire() for entry in self.errors],
            "warnings": [entry.to_wire() for entry in self.warnings],
            "suggestions": [entry.to_wire() for entry in self.suggestions],
            "semantic_contracts": [],
            "graph_repair_suggestions": [],
        }


@dataclass(frozen=True, slots=True)
class ClosedProviderDiscoveryEnvelope:
    success: bool
    validation: ClosedProviderValidation
    affected_nodes: tuple[str, ...]
    version: int
    data: AdmittedResponse | None

    def to_wire(self) -> dict[str, JsonValue]:
        wire: dict[str, JsonValue] = {
            "success": self.success,
            "validation": self.validation.to_wire(),
            "affected_nodes": list(self.affected_nodes),
            "version": self.version,
        }
        if self.data is not None:
            wire["data"] = self.data.to_wire()
        return wire


def _validation_entry(entry: ValidationEntry, fallback: str) -> ClosedProviderValidationEntry:
    return ClosedProviderValidationEntry(entry.severity, entry.error_code or fallback)


def closed_provider_envelope(
    result: ToolResult, *, success: bool | None = None, data: AdmittedResponse | None = None
) -> ClosedProviderDiscoveryEnvelope:
    """Construct the final envelope without reading or traversing result.data."""
    validation = result.validation
    return ClosedProviderDiscoveryEnvelope(
        result.success if success is None else success,
        ClosedProviderValidation(
            validation.is_valid,
            tuple(_validation_entry(entry, "validation_error") for entry in validation.errors),
            tuple(_validation_entry(entry, "validation_warning") for entry in validation.warnings),
            tuple(_validation_entry(entry, "validation_suggestion") for entry in validation.suggestions),
        ),
        tuple(result.affected_nodes),
        result.updated_state.version,
        data,
    )


@dataclass(frozen=True, slots=True)
class _ProjectionFailure(_UncachedProjection):
    kind: Literal["schema_unavailable", "schema_budget"]

    def to_wire(self) -> dict[str, JsonValue]:
        if self.kind == "schema_unavailable":
            message = "The selected plugin schema cannot be represented in the bounded planner projection. Use get_plugin_assistance."
            code = "schema_projection_unavailable"
        else:
            message = "The selected plugin contracts exceed the aggregate planner schema budget. Use get_plugin_assistance."
            code = "schema_contract_budget_exceeded"
        return {"error": message, "error_code": code, "next_tool": "get_plugin_assistance"}


def schema_projection_failure() -> AdmittedResponse:
    return _ProjectionFailure("schema_unavailable")


def schema_budget_failure() -> AdmittedResponse:
    return _ProjectionFailure("schema_budget")

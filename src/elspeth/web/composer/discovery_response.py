"""Admitted discovery data carried separately from its current tool envelope."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import cast

from pydantic import JsonValue

from elspeth.contracts.errors import FrameworkBugError
from elspeth.web.composer.response_contracts import AdmittedResponse, ResponseContract
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.composer.tools._registry import response_contract_for


@dataclass(frozen=True, slots=True)
class AdmittedDiscoveryResult:
    """Envelope plus selected data; original data is never traversed on egress."""

    result: ToolResult
    response: AdmittedResponse | None
    contract: ResponseContract

    def to_tool_result(self) -> ToolResult:
        """Project admitted data for legacy outcome consumers and persistence."""
        if self.response is None:
            return replace(self.result, data=None)
        wire = self.response.to_wire()
        if not isinstance(wire, dict | list):
            raise FrameworkBugError("Discovery response encoder produced an invalid root")
        return replace(self.result, data=wire)

    def to_dict(self) -> dict[str, JsonValue]:
        # Build only the legacy envelope, with no traversal of original data.
        envelope = cast(dict[str, JsonValue], self.result.to_dict(include_data=False))
        if self.response is None:
            return envelope
        payload: dict[str, JsonValue] = {}
        for key, value in envelope.items():
            payload[key] = value
            if key == "version":
                payload["data"] = self.response.to_wire()
        return payload


def admit_discovery_result(tool_name: str, result: ToolResult) -> AdmittedDiscoveryResult:
    contract = response_contract_for(tool_name)
    if contract is None:
        raise FrameworkBugError("Discovery declaration has no response contract")
    if result.success:
        if result.data is None:
            raise FrameworkBugError("Successful discovery response has no data")
        response = contract.admit(result.data)
    else:
        if result.data is not None:
            raise FrameworkBugError("Failed discovery response has unexpected data")
        response = None
    return AdmittedDiscoveryResult(result, response, contract)


def serialize_admitted_discovery_result(value: AdmittedDiscoveryResult) -> str:
    return json.dumps(value.to_dict())

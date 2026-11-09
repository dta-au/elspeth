"""Admitted discovery data carried separately from its current tool envelope."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import cast

from pydantic import JsonValue

from elspeth.contracts.errors import FrameworkBugError
from elspeth.web.composer._producer_resolver import source_producer_id
from elspeth.web.composer.response_contracts import AdmittedResponse, ResponseContract
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.composer.tools._generation_responses import PREVIEW_PIPELINE_RESPONSE_CONTRACT
from elspeth.web.composer.tools._registry import response_contract_for
from elspeth.web.composer.tools.generation import MalformedSourceProofClaim, ProofUnavailable, _admit_source_proof_claims


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


def preview_source_proof_stop(
    admitted: AdmittedDiscoveryResult,
    state: CompositionState,
) -> ProofUnavailable | MalformedSourceProofClaim | None:
    """Recognize only this preview producer's current saved-source refusal.

    Ordinary standing errors and candidate refusals carry no stop authority.
    Re-admission uses the selected closed preview contract, never tool prose.
    """
    if admitted.contract is not PREVIEW_PIPELINE_RESPONSE_CONTRACT or admitted.result.updated_state is not state:
        return None
    result = admitted.result
    if not result.success:
        if (
            admitted.response is not None
            or result.data is not None
            or result.validation.is_valid
            or result.affected_nodes
            or result._state_validation_withheld
            or not result.validation.errors
        ):
            return None
        rejection = result.validation.errors[0]
        if (
            rejection.component != "rejected_mutation"
            or rejection.severity != "high"
            or rejection.error_code != "source_blob_identity_invalid"
        ):
            return None
        try:
            _admit_source_proof_claims(state)
        except MalformedSourceProofClaim as exc:
            return exc if rejection.rejected_component == source_producer_id(exc.source_name) else None
        return None
    if admitted.response is None:
        raise FrameworkBugError("Successful admitted preview omitted its response")
    preview = PREVIEW_PIPELINE_RESPONSE_CONTRACT.parse(admitted.response.to_wire())
    if preview.preview_is_valid:
        return None
    try:
        claims = {name: blob_id for name, _source, blob_id in _admit_source_proof_claims(state)}
    except MalformedSourceProofClaim:
        return None
    unavailable = tuple(
        diagnostic
        for diagnostic in preview.proof_diagnostics
        if diagnostic.code == "source_blob_unavailable" and diagnostic.severity == "blocking"
    )
    if not unavailable or any(
        diagnostic.evidence_locator.source_name not in claims
        or diagnostic.evidence_locator.blob_id != claims[diagnostic.evidence_locator.source_name]
        for diagnostic in unavailable
    ):
        return None
    return ProofUnavailable(tuple(diagnostic.model_dump() for diagnostic in preview.proof_diagnostics))

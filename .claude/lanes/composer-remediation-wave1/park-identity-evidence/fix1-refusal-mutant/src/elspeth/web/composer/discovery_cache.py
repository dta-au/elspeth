"""Discovery result cache and tool-result serialization helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.prompt_guard import PromptGuardContentPart
from elspeth.web.catalog.schemas import PluginSchemaInfo
from elspeth.web.composer._attachment_response import AdmittedAttachmentResponse
from elspeth.web.composer.discovery_response import AdmittedDiscoveryResult
from elspeth.web.composer.prompt_guard_rendering import JsonPath, json_content_parts
from elspeth.web.composer.response_contracts import AdmittedResponse
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import ToolResult
from elspeth.web.composer.tools._generation_schema_response import AdmittedPluginSchemaResponse
from elspeth.web.composer.tools._registry import response_contract_for
from elspeth.web.execution.runtime_preflight import RuntimePreflightEntry, RuntimePreflightKey


@dataclass(frozen=True, slots=True)
class CachedDiscoveryPayload:
    """State-independent portion of a cacheable discovery tool result."""

    success: bool
    affected_nodes: tuple[str, ...]
    data: Any
    admitted_response: AdmittedResponse | None = None


RuntimePreflightCache = dict[RuntimePreflightKey, RuntimePreflightEntry]


def tool_result_mutated_composition_state(
    *,
    version_before: int,
    result: ToolResult,
) -> bool:
    """Return True when a successful tool advanced the CompositionState version."""
    return result.success and result.updated_state.version > version_before


def pydantic_default(obj: Any) -> Any:
    """JSON serializer fallback for Pydantic models in tool results."""
    if not isinstance(obj, BaseModel):
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable") from None
    return obj.model_dump()


def serialize_tool_result(result: ToolResult) -> str:
    """Serialize a ToolResult to JSON, handling Pydantic models in data."""
    return json.dumps(result.to_dict(), default=pydantic_default)


def tool_result_content_parts(result: ToolResult | AdmittedDiscoveryResult, serialized: str) -> tuple[PromptGuardContentPart, ...]:
    """Bind reference authority to nominal current producer data, not JSON keys."""
    references: set[JsonPath] = set()
    string_parts: dict[JsonPath, tuple[PromptGuardContentPart, ...]] = {}
    if isinstance(result, AdmittedDiscoveryResult):
        if result.result.success and type(result.response) is AdmittedAttachmentResponse:
            string_parts[("data", "content")] = result.response.untrusted_content_parts()
        if result.result.success and type(result.response) is AdmittedPluginSchemaResponse:
            references.add(("data",))
        envelope = result.result
    else:
        if result.success and type(result.data) is PluginSchemaInfo:
            references.add(("data",))
        envelope = result
    # This field is created only by the policy-bound failure augmentation,
    # not admitted from model tool arguments or a serialized response.
    if envelope.plugin_schemas is not None:
        references.add(("plugin_schemas",))
    return json_content_parts(serialized, reference_paths=frozenset(references), string_parts=string_parts)


def cached_discovery_payload(result: ToolResult | AdmittedDiscoveryResult) -> CachedDiscoveryPayload:
    """Extract the state-independent fields from a cacheable discovery result."""
    if isinstance(result, AdmittedDiscoveryResult):
        if not result.result.success or result.response is None:
            raise FrameworkBugError("Only successful discovery data can be cached")
        return CachedDiscoveryPayload(True, result.result.affected_nodes, None, result.response)
    return CachedDiscoveryPayload(
        success=result.success,
        affected_nodes=result.affected_nodes,
        data=result.data,
    )


def result_from_cached_discovery_payload(
    state: CompositionState,
    cached: CachedDiscoveryPayload,
) -> ToolResult:
    """Rebuild a cached discovery result with the current state envelope."""
    return ToolResult(
        success=cached.success,
        updated_state=state,
        validation=state.validate(),
        affected_nodes=cached.affected_nodes,
        data=cached.data,
    )


def admitted_result_from_cached_discovery_payload(
    tool_name: str,
    state: CompositionState,
    cached: CachedDiscoveryPayload,
) -> AdmittedDiscoveryResult:
    """Re-admit canonical cached data before any success audit or disclosure."""
    contract = response_contract_for(tool_name)
    if contract is None or cached.admitted_response is None or cached.success is not True or cached.data is not None:
        raise FrameworkBugError("Malformed admitted discovery cache entry")
    response = cached.admitted_response.readmit(contract)
    result = result_from_cached_discovery_payload(state, cached)
    return AdmittedDiscoveryResult(result, response, contract)


def make_cache_key(tool_name: str, arguments: dict[str, Any]) -> str:
    """Build a deterministic cache key from tool name + arguments."""
    return f"{tool_name}:{json.dumps(arguments, sort_keys=True)}"

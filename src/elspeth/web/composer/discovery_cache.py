"""Discovery result cache and tool-result serialization helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.prompt_guard import PromptGuardContentPart
from elspeth.contracts.secrets import SecretInventoryItem
from elspeth.web.catalog.schemas import PluginSchemaInfo
from elspeth.web.catalog.validation_reuse import MutationEpoch
from elspeth.web.composer._attachment_response import AdmittedAttachmentResponse
from elspeth.web.composer.discovery_response import AdmittedDiscoveryResult
from elspeth.web.composer.prompt_guard_rendering import JsonPath, json_content_parts
from elspeth.web.composer.response_contracts import AdmittedResponse
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import ToolResult
from elspeth.web.composer.tools._generation_schema_response import AdmittedPluginSchemaResponse
from elspeth.web.composer.tools._registry import response_contract_for
from elspeth.web.execution.runtime_preflight import RuntimePreflightEntry, RuntimePreflightKey
from elspeth.web.execution.schemas import ValidationResult


@dataclass(frozen=True, slots=True)
class CachedDiscoveryPayload:
    """State-independent portion of a cacheable discovery tool result."""

    success: bool
    affected_nodes: tuple[str, ...]
    data: Any
    admitted_response: AdmittedResponse | None = None


RuntimePreflightCache = dict[RuntimePreflightKey, RuntimePreflightEntry]


@dataclass(frozen=True, slots=True)
class BlobEvidence:
    """One referenced blob as a runtime preflight would read it.

    ``record`` is the whole metadata row (every field the materialiser
    compares is in it). The stat fields catch stored bytes rewritten without
    a record change, which the uncached path catches by re-hashing; the
    change time is among them because a writer cannot set it back, so a
    same-size rewrite with the modification time restored still shows.
    """

    record: BlobRecord
    storage_size: int
    storage_mtime_ns: int
    storage_ctime_ns: int
    storage_inode: int


ResolvedLocalPath = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class PreflightEvidence:
    """Every input a runtime preflight reads that can change between two calls.

    ``blobs`` holds each referenced blob in id order. ``secret_inventory`` is
    the user's inventory as ``secret_service.list_refs`` returns it (names and
    availability, never values), which the preflight reads to judge
    ``${NAME}`` credential values; a secret created or deleted over HTTP never
    passes through a composer tool, so no mutation epoch sees it. A changed
    value under an unchanged name is not in the inventory: a state whose
    options wire a secret is therefore never reused at all. ``local_paths``
    holds each source, sink and ``provider_config`` path as the path
    allowlist resolves it (symbolic links followed), as
    ``(component, option key, resolved path or error class)``.
    """

    blobs: tuple[BlobEvidence, ...]
    secret_inventory: frozenset[SecretInventoryItem]
    local_paths: tuple[ResolvedLocalPath, ...]


@dataclass(frozen=True, slots=True)
class BlobBoundPreflightVerdict:
    """A returned verdict for a blob-bearing state, with what it was judged on.

    Reused only for the identical state object, in the same mutation epoch,
    while a fresh read of the evidence still equals ``evidence``. ``result``
    is a private deep copy; every reuse hands out another deep copy, so no
    two callers share one mutable result. Failures are never stored here.
    """

    state: CompositionState
    mutation_epoch: int
    evidence: PreflightEvidence
    result: ValidationResult


class ComposeRoundPreflightCache(RuntimePreflightCache):
    """One compose call's runtime-preflight cache.

    The mapping itself is the existing cache for states without a blob
    reference (returned verdicts and failures, keyed by
    ``RuntimePreflightKey``). ``blob_verdicts`` is the separate store for
    blob-bearing states, which the mapping never holds because blob status can
    change without a state version change. ``mutation_epoch`` is the round
    view's epoch, shared so a mutation-kind tool call ends every reuse.
    """

    __slots__ = ("blob_verdicts", "mutation_epoch")

    def __init__(self, *, mutation_epoch: MutationEpoch) -> None:
        super().__init__()
        self.blob_verdicts: dict[RuntimePreflightKey, BlobBoundPreflightVerdict] = {}
        self.mutation_epoch = mutation_epoch


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

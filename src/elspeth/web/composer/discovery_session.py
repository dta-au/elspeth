"""Shared discovery retention with argument admission and fresh envelopes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import JsonValue
from rfc8785 import CanonicalizationError

from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.discovery_cache import (
    CachedDiscoveryPayload,
    admitted_result_from_cached_discovery_payload,
    cached_discovery_payload,
    make_cache_key,
)
from elspeth.web.composer.discovery_response import AdmittedDiscoveryResult, admit_discovery_result
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.session_cache import DiscoveryKey, MemoSource, RegistryRevision, discovery_weight_bytes
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import ToolResult
from elspeth.web.composer.tools._common import _validate_mutation_arguments
from elspeth.web.composer.tools._dispatch import _validate_tool_arguments
from elspeth.web.composer.tools._registry import declaration_for
from elspeth.web.composer.tools.declarations import DiscoveryCacheScope


@dataclass(frozen=True, slots=True)
class DiscoveryUse:
    admitted: AdmittedDiscoveryResult
    cache_hit: bool


@dataclass(slots=True)
class DiscoveryAudit:
    cache_hit: bool = False

    def was_hit(self) -> bool:
        return self.cache_hit


def admitted_discovery_key(
    tool_name: str, arguments: dict[str, JsonValue], state: CompositionState, catalog: PolicyCatalogView
) -> str | None:
    """Run the declaration's owning parser before any retained lookup."""
    declaration = declaration_for(tool_name)
    if not declaration.cacheable or declaration.argument_model is None or not declaration.cache_revision:
        return None
    _validate_tool_arguments(tool_name, arguments, state, raise_on_error=True, catalog=catalog)
    _validate_mutation_arguments(declaration.argument_model, arguments, f"{tool_name} arguments")
    # Assistance dispatch reads the shared manager directly. A custom catalog
    # backed by another manager cannot establish that producer dependency.
    if tool_name == "get_plugin_assistance" and not catalog.uses_manager(get_shared_plugin_manager()):
        return None
    try:
        return make_cache_key(
            f"{tool_name}:{declaration.cache_revision}:{catalog.projection_revision}:{catalog.snapshot.snapshot_hash}", arguments
        )
    except CanonicalizationError:
        # A declaration can admit an integer outside JCS's safe domain. Cache
        # eligibility must not narrow its handler's public argument contract.
        return None


def operation_discovery_key(
    tool_name: str, arguments: dict[str, JsonValue], state: CompositionState, catalog: PolicyCatalogView
) -> str | None:
    """Defer malformed calls to the ordinary audited argument-error dispatch."""
    try:
        return admitted_discovery_key(tool_name, arguments, state, catalog)
    except ToolArgumentError:
        return None


def execute_cached_discovery(
    tool_name: str,
    arguments: dict[str, JsonValue],
    state: CompositionState,
    catalog: PolicyCatalogView,
    compute: Callable[[], ToolResult],
    operation_cache: dict[str, CachedDiscoveryPayload] | None = None,
) -> DiscoveryUse:
    """Retain only admitted success data; each caller owns its current envelope.

    Caller budget/repeat classification remains operation-local. Physical hits
    report separately and do not themselves confer a free discovery turn.
    """
    declaration = declaration_for(tool_name)
    operation_key = admitted_discovery_key(tool_name, arguments, state, catalog)
    if operation_key is None:
        return DiscoveryUse(admit_discovery_result(tool_name, compute()), False)
    if operation_key is not None and operation_cache is not None and operation_key in operation_cache:
        return DiscoveryUse(
            admitted_result_from_cached_discovery_payload(tool_name, state, operation_cache[operation_key], catalog=catalog), True
        )
    revision = catalog.projection_revision
    cache = catalog.analysis.cache
    computed: AdmittedDiscoveryResult | None = None

    def produce() -> CachedDiscoveryPayload:
        nonlocal computed
        computed = admit_discovery_result(tool_name, compute())
        if not computed.result.success:
            # Failure is shared only as a non-retained miss marker. A joining
            # caller executes its own failure path for its own state/envelope.
            return CachedDiscoveryPayload(False, (), None)
        return cached_discovery_payload(computed)

    hit = False
    if (
        operation_key is not None
        and declaration.discovery_cache_scope is DiscoveryCacheScope.SESSION
        and cache is not None
        and revision is not None
    ):
        manager = get_shared_plugin_manager()
        registry = RegistryRevision(manager, tuple(manager.get_sources()), tuple(manager.get_transforms()), tuple(manager.get_sinks()))
        key = DiscoveryKey(tool_name, operation_key, f"{revision}:{declaration.cache_revision}", catalog.snapshot.snapshot_hash, registry)
        memo = cache.discovery.get_or_compute(
            key,
            produce,
            key_weight=key.weight_bytes(),
            value_weight=discovery_weight_bytes,
            retain=lambda _: catalog.projection_revision == revision,
        )
        payload = memo.value
        hit = memo.source in (MemoSource.HIT, MemoSource.JOIN) and payload.success
    else:
        payload = produce()
    if not payload.success:
        return DiscoveryUse(computed if computed is not None else admit_discovery_result(tool_name, compute()), False)
    if operation_key is not None and operation_cache is not None:
        operation_cache[operation_key] = payload
    return DiscoveryUse(
        computed if computed is not None else admitted_result_from_cached_discovery_payload(tool_name, state, payload, catalog=catalog), hit
    )

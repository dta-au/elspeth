"""Shared serialization + assistance helpers for semantic contracts.

These helpers serve every consumer that needs to render or annotate the
output of ``validate_semantic_contracts`` — currently:

* ``elspeth.web.execution.validation`` — /validate response payload.
* ``elspeth.web.execution.errors`` (via SemanticContractViolationError) —
  structured exception carried out of /execute when semantic contracts
  reject a pipeline.
* ``elspeth.web.execution.routes`` — 422 handler that turns
  SemanticContractViolationError into a JSON payload.

Hoisting the two helpers out of ``validation.py`` keeps every surface
that renders semantic contracts on a single source of truth — adding a
field updates one site rather than three. The shapes consciously
mirror ``elspeth.composer_mcp.server._SemanticEdgeContractPayload`` so
HTTP, MCP, and exception-path payloads stay aligned.
"""

from __future__ import annotations

from typing import cast

from elspeth.contracts.plugin_semantics import SemanticEdgeContract
from elspeth.web.composer.state import ValidationEntry
from elspeth.web.execution.schemas import SemanticEdgeContractResponse


def serialize_semantic_contracts(
    contracts: tuple[SemanticEdgeContract, ...],
) -> list[SemanticEdgeContractResponse]:
    """Convert internal SemanticEdgeContract records to the wire response model.

    Field shape mirrors composer_mcp/server.py::_SemanticEdgeContractPayload.
    Operators want to confirm "yes, semantic_contracts: 1 satisfied" in the
    UI banner even on success paths — the response carries the same
    structured payload regardless of the overall pass/fail outcome.
    """
    return [
        SemanticEdgeContractResponse(
            from_id=c.from_id,
            to_id=c.to_id,
            consumer_plugin=c.consumer_plugin,
            producer_plugin=c.producer_plugin,
            producer_field=c.producer_field,
            consumer_field=c.consumer_field,
            outcome=c.outcome.value,
            requirement_code=c.requirement.requirement_code,
        )
        for c in contracts
    ]


# Sink consumers are addressed as ``output:<name>`` on both the validation
# entry and the semantic contract, matching the id the schema-contract layer
# already gives a sink edge (``EdgeContract.to_id``). Transform nodes are
# addressed by bare node id, which cannot carry this prefix — composer node ids
# and connection names are validated as disjoint from sink names, and
# ``output:`` is not a legal node id.
_SINK_COMPONENT_PREFIX = "output:"


def _is_sink_contract(contract: SemanticEdgeContract) -> bool:
    """Return whether a semantic contract's consumer is a sink, not a transform."""
    return contract.to_id.startswith(_SINK_COMPONENT_PREFIX)


def semantic_component_attribution(component: str) -> tuple[str, str]:
    """Split a semantic ValidationEntry component into (component_id, component_type).

    ``node:<id>`` -> (``<id>``, "transform"); ``output:<name>`` ->
    (``<name>``, "sink"). Hardcoding "transform" here mis-attributed every
    sink finding in the validation ledger to a node that does not exist.
    """
    if component.startswith(_SINK_COMPONENT_PREFIX):
        return component.removeprefix(_SINK_COMPONENT_PREFIX), "sink"
    return component.removeprefix("node:"), "transform"


def semantic_affected_component_id(component: str) -> str:
    """Return the id to record in ``affected_nodes`` for a semantic finding.

    A transform node contributes its bare id; a sink KEEPS its ``output:``
    qualifier. ``affected_nodes`` carries no type column, so dropping the
    qualifier would make a sink advisory indistinguishable from a node
    advisory — and a sink sharing a name with a node would silently merge
    with it. ``output:<name>`` is the same qualified id the schema-contract
    layer uses, so the two evidence surfaces stay readable together.
    """
    return component.removeprefix("node:")


def assistance_suggestion_for(
    entry: ValidationEntry,
    contracts: tuple[SemanticEdgeContract, ...],
) -> str | None:
    """Look up plugin-owned guidance for a semantic error.

    Uses SemanticEdgeContract.consumer_plugin (and producer_plugin as
    a fallback) to address a SPECIFIC plugin class. Looping every
    registered transform and returning the first match was registry-
    order dependent — fixed by carrying the plugin names on the
    contract (Phase 1 Task 1.3).
    """
    from elspeth.plugins.infrastructure.base import BaseSink, BaseTransform
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

    # A node entry is ``node:<id>`` against a contract ``to_id`` of ``<id>``; a
    # sink entry is ``output:<name>`` on both sides (the id convention the
    # schema-contract layer already uses for a sink edge). Stripping only the
    # ``node:`` prefix therefore matches both.
    component_id = entry.component.removeprefix("node:")
    matching = next((c for c in contracts if c.to_id == component_id), None)
    if matching is None:
        return None

    manager = get_shared_plugin_manager()
    issue_code = matching.requirement.requirement_code

    # Consumer plugin owns the requirement, so it's the authoritative
    # source for guidance about the requirement_code. Sinks and transforms
    # live in SEPARATE registries under names that may collide, so the
    # lookup must be routed by what the consumer actually is — asking
    # get_transform_by_name for the "text" sink raises PluginNotFoundError.
    # Verified method names: get_transform_by_name (manager.py:183) and
    # get_sink_by_name (manager.py:200). Both registries return protocol
    # types; assistance lives on BaseTransform/BaseSink and every in-tree
    # plugin subclasses one of them, so the casts are sound (per CLAUDE.md
    # plugin-as-system-code policy).
    consumer_assistance = (
        cast(type[BaseSink], manager.get_sink_by_name(matching.consumer_plugin)).get_agent_assistance(issue_code=issue_code)
        if _is_sink_contract(matching)
        else cast(type[BaseTransform], manager.get_transform_by_name(matching.consumer_plugin)).get_agent_assistance(issue_code=issue_code)
    )
    if consumer_assistance is not None:
        return consumer_assistance.summary

    # Producer plugin may also publish guidance for the producer-side
    # fact_code. The validator could attach that fact_code on the
    # contract in a later phase; for now, only consumer assistance is
    # surfaced as suggestion text.
    return None

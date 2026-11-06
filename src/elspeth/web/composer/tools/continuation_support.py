"""Prospective continuation support; never dispatch or current-use authority."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from elspeth.web.composer.tools._registry import ASYNC_TOOL_EFFECTS, continuation_declaration, resolve_tool_effects
from elspeth.web.composer.tools.declarations import EffectDomain, ToolKind

ContinuationToolSupport = Literal["read", "graph", "advisor", "session_sql", "unsupported"]


def continuation_tool_support(tool_name: str, arguments: Mapping[str, Any]) -> ContinuationToolSupport:
    if tool_name == "request_advisor_hint":
        effects = ASYNC_TOOL_EFFECTS.get(tool_name)
        return "advisor" if effects is not None and not effects.domains else "unsupported"
    if tool_name == "request_interpretation_review":
        effects = ASYNC_TOOL_EFFECTS.get(tool_name)
        return "session_sql" if effects is not None and effects.domains == (EffectDomain.INTERPRETATION,) else "unsupported"
    if tool_name == "preview_pipeline":
        return "unsupported"
    declaration = continuation_declaration(tool_name)
    if declaration is None:
        return "unsupported"
    domains = resolve_tool_effects(tool_name, arguments).domains
    if declaration.kind is ToolKind.BLOB_DISCOVERY:
        if (
            tool_name in ("list_blobs", "list_composer_blobs", "get_blob_metadata", "get_blob_content", "read_attachment", "inspect_source")
            and not domains
            and declaration.response_contract is not None
        ):
            return "read"
        return "unsupported"
    if declaration.kind is ToolKind.BLOB_MUTATION:
        return "unsupported"
    if any(domain not in (EffectDomain.GRAPH, EffectDomain.VALIDATION, EffectDomain.YAML) for domain in domains):
        return "unsupported"
    if domains:
        return "graph"
    if declaration.kind in (ToolKind.DISCOVERY, ToolKind.SECRET_DISCOVERY) and declaration.response_contract is not None:
        return "read"
    return "unsupported"

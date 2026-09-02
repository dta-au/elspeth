"""The closed ToolResult wire vocabulary — one authority for producer, redaction, and planner twins.

``ToolResult.to_dict`` (``tools/_common.py``) emits exactly these top-level keys;
``redaction._ToolResultResponseModel`` and ``redaction._tool_result_response_keys``
admit exactly these; ``pipeline_planner._ClosedProviderDiscoveryPayload`` is a
subset of these. ``tests/unit/web/composer/test_tool_result_envelope_gate.py``
pins all three against this module from the AST and the live objects, so a key
added in one place and not the others turns the tree red (elspeth-e405ad7cd2).

This module imports nothing from ``elspeth.web``: both ``redaction`` and
``tools/_common`` import it, and ``tools/__init__`` imports ``_dispatch``, which
imports ``redaction`` — so the registry cannot live under ``tools/``.
"""

from __future__ import annotations

from typing import Final, NotRequired, TypedDict

TOOL_RESULT_REQUIRED_KEYS: Final[tuple[str, ...]] = (
    "success",
    "validation",
    "affected_nodes",
    "version",
)
"""Present on every serialized ToolResult, in emission order."""

TOOL_RESULT_OPTIONAL_KEYS: Final[tuple[str, ...]] = (
    "data",
    "runtime_preflight",
    "validation_delta",
    "post_call_hints",
    "plugin_schemas",
    "validation_guidance",
    "applied_component",
)
"""Emitted only when set / non-empty, in emission order."""

TOOL_RESULT_POST_DISPATCH_KEYS: Final[tuple[str, ...]] = (
    "pipeline_content_hash_schema",
    "pipeline_content_hash",
)
"""Attached after dispatch by ``pipeline_commit`` (set_pipeline only); never emitted by ``to_dict``."""

VALIDATION_KEYS: Final[tuple[str, ...]] = (
    "is_valid",
    "errors",
    "warnings",
    "suggestions",
    "semantic_contracts",
    "graph_repair_suggestions",
)
"""Keys of the ``validation`` sub-envelope, in emission order."""

VALIDATION_DELTA_KEYS: Final[tuple[str, ...]] = (
    "new_errors",
    "resolved_errors",
    "new_warnings",
    "resolved_warnings",
)
"""Keys of ``validation_delta`` (``_compute_validation_delta``), in emission order."""

APPLIED_COMPONENT_KEYS: Final[tuple[str, ...]] = (
    "source",
    "sources",
    "nodes",
    "outputs",
    "edges",
)
"""Keys of ``applied_component`` (``_applied_component_echo``), in emission order; each only when non-empty."""


def tool_result_keys(*, data: bool) -> tuple[str, ...]:
    """The top-level envelope in emission order, with or without ``data``."""
    optional = TOOL_RESULT_OPTIONAL_KEYS if data else tuple(key for key in TOOL_RESULT_OPTIONAL_KEYS if key != "data")
    return (*TOOL_RESULT_REQUIRED_KEYS, *optional)


class ValidationCodeGuidance(TypedDict):
    """The catalogue's ``(explanation, suggested_fix)`` for one closed code."""

    explanation: str
    suggested_fix: str


class ValidationGuidance(TypedDict):
    """Inline repair guidance for one failed mutation envelope.

    ``codes`` is keyed by the closed ``error_code`` so N entries sharing a
    code cost the text once. ``explain_tool`` rides only when some entry got
    no inline guidance — see ``tools.generation.build_validation_guidance``.
    """

    codes: dict[str, ValidationCodeGuidance]
    explain_tool: NotRequired[str]

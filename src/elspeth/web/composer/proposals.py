"""Plain-language summaries for pending composer tool proposals."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from elspeth.contracts.freeze import freeze_fields
from elspeth.web.composer.redaction import SetPipelineArgumentsModel
from elspeth.web.composer.tools._common import _validate_mutation_arguments
from elspeth.web.composer.tools._registry import resolve_tool_effects


@dataclass(frozen=True, slots=True)
class ToolProposalSummary:
    summary: str
    rationale: str
    affects: tuple[str, ...]
    arguments_redacted_json: Mapping[str, Any]

    def __post_init__(self) -> None:
        freeze_fields(self, "affects", "arguments_redacted_json")


def _plural(count: int, singular: str) -> str:
    return f"{count} {singular}" if count == 1 else f"{count} {singular}s"


def _string_argument(arguments: Mapping[str, Any], key: str) -> str | None:
    if key not in arguments:
        return None
    value = arguments[key]
    return value if type(value) is str and value and not value.startswith("<redacted") else None


def build_tool_proposal_summary(
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
    redacted_arguments: Mapping[str, Any],
) -> ToolProposalSummary:
    rationale = "Requested by the current composer turn."
    affects = tuple(domain.value for domain in resolve_tool_effects(tool_name, arguments).domains)

    if tool_name == "set_pipeline":
        validated = _validate_mutation_arguments(SetPipelineArgumentsModel, arguments, "set_pipeline arguments")
        source_count = len(validated.sources) if validated.sources is not None else 1
        node_count = len(validated.nodes)
        output_count = len(validated.outputs)
        return ToolProposalSummary(
            summary=(
                f"Replace the pipeline with {_plural(source_count, 'input')}, "
                f"{_plural(node_count, 'processing node')}, and {_plural(output_count, 'output')}."
            ),
            rationale=rationale,
            affects=affects,
            arguments_redacted_json=redacted_arguments,
        )

    if tool_name == "set_source":
        label = _string_argument(redacted_arguments, "plugin") or "source"
        return ToolProposalSummary(
            summary=f"Set the pipeline source to {label}.",
            rationale=rationale,
            affects=affects,
            arguments_redacted_json=redacted_arguments,
        )

    if tool_name == "patch_node_options":
        label = _string_argument(redacted_arguments, "node_id") or "selected node"
        return ToolProposalSummary(
            summary=f'Update options for node "{label}".',
            rationale=rationale,
            affects=affects,
            arguments_redacted_json=redacted_arguments,
        )

    if tool_name == "patch_source_options":
        return ToolProposalSummary(
            summary="Update source options.",
            rationale=rationale,
            affects=affects,
            arguments_redacted_json=redacted_arguments,
        )

    if tool_name == "patch_output_options":
        label = _string_argument(redacted_arguments, "sink_name") or "selected output"
        return ToolProposalSummary(
            summary=f'Update options for output "{label}".',
            rationale=rationale,
            affects=affects,
            arguments_redacted_json=redacted_arguments,
        )

    if tool_name == "request_interpretation_review":
        # Phase 5b Task 5 — surface-for-user-review proposals. The action
        # is structurally different from graph/validation/yaml-affecting
        # tools: composition state version does NOT advance until the
        # user resolves the event at /resolve time. The ``affects`` slot
        # carries a new value, ``"interpretation"``, so any frontend
        # surface listing affected subsystems can disambiguate this row.
        # The frontend type ``affects: string[]`` (web/frontend/src/types/index.ts)
        # is intentionally open — no closed literal to extend.
        return ToolProposalSummary(
            summary="Surface an interpretation draft for user review.",
            rationale="Review the planner's proposed interpretation or assumption before it is accepted into the pipeline.",
            affects=affects,
            arguments_redacted_json=redacted_arguments,
        )

    if tool_name in {"create_blob", "update_blob", "delete_blob"}:
        verb = {"create_blob": "Create", "update_blob": "Overwrite", "delete_blob": "Delete"}[tool_name]
        blob_id = _string_argument(redacted_arguments, "blob_id")
        target = f'session file "{blob_id}"' if blob_id else "a session file"
        summary = f"{verb} {target}."
    elif tool_name == "request_advisor_hint":
        summary = "Request advisor guidance."
    elif not affects:
        summary = "Inspect composer information."
    else:
        summary = f"Apply composer tool {tool_name}."
    return ToolProposalSummary(
        summary=summary,
        rationale=rationale,
        affects=affects,
        arguments_redacted_json=redacted_arguments,
    )

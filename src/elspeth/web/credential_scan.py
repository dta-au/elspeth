"""The best-effort credential scan at refusing sites, and its skip count.

A match refuses. A scan that stops at a traversal limit, or meets a value it
cannot traverse, lets the value through and counts the skip. The count is an
operational fact about the control's coverage, not a run's data outcome, so
it goes to telemetry, value-free, with two closed attributes.
"""

from __future__ import annotations

from typing import Any, Final, Literal, get_args

from opentelemetry import metrics

from elspeth.contracts.credential_material import (
    DEFAULT_CREDENTIAL_TRAVERSAL_POLICY,
    CredentialMaterialFinding,
    CredentialTraversalPolicy,
    find_credential_material,
    is_credential_match,
)

CredentialScanSurface = Literal[
    "advisor_historical_read_recovery",
    "aws_ecs_bedrock_response",
    "blob_filename",
    "composer_advisor_complete_content",
    "composer_advisor_response",
    "composer_blob_content",
    "composer_blob_metadata",
    "composer_library_fork_state",
    "composer_mcp_tool_arguments",
    "composer_planner_response",
    "composer_prompt_guard_request",
    "composer_provider_request",
    "composer_provider_response",
    "composer_provider_thinking",
    "composer_read_attachment",
    "composer_recovery_reply",
    "composer_recovery_request",
    "composer_source_inspection",
    "composer_tool_arguments",
    "composer_tool_batch",
    "composer_tool_wire",
    "composer_visible_tool_persistence",
    "composer_yaml_import",
    "session_title",
    "web_fork_message_ingress",
    "web_message_ingress",
    "web_request_validation",
]
CredentialScanSkipReason = Literal["depth", "nodes", "width", "string_bytes", "reference_bytes", "unsupported_value"]

CREDENTIAL_SCAN_SURFACES: Final[frozenset[str]] = frozenset(get_args(CredentialScanSurface))
CREDENTIAL_SCAN_SKIP_REASONS: Final[frozenset[str]] = frozenset(get_args(CredentialScanSkipReason))

_CREDENTIAL_SCAN_SKIPPED_COUNTER = metrics.get_meter(__name__).create_counter(
    "composer.credential_scan.skipped",
    unit="1",
    description="Count of best-effort credential scans that let a value through at a traversal limit or an unsupported value.",
)


def credential_scan_skip_reason(finding: CredentialMaterialFinding) -> CredentialScanSkipReason:
    """Return the closed skip reason for a finding that is not a match."""
    if finding.category == "unsupported_value":
        return "unsupported_value"
    if finding.category == "traversal_limit" and finding.bound is not None:
        return finding.bound
    raise ValueError("Only a traversal limit or an unsupported value is a credential-scan skip")


def record_credential_scan_skipped(*, surface: CredentialScanSurface, reason: CredentialScanSkipReason) -> None:
    """Count one skipped scan. Both attributes come from closed sets."""
    if surface not in CREDENTIAL_SCAN_SURFACES:
        raise ValueError("Credential-scan skip surface is not in the closed set")
    if reason not in CREDENTIAL_SCAN_SKIP_REASONS:
        raise ValueError("Credential-scan skip reason is not in the closed set")
    _CREDENTIAL_SCAN_SKIPPED_COUNTER.add(1, {"surface": surface, "reason": reason})


def scan_best_effort(
    value: Any,
    *,
    surface: CredentialScanSurface,
    policy: CredentialTraversalPolicy = DEFAULT_CREDENTIAL_TRAVERSAL_POLICY,
) -> CredentialMaterialFinding | None:
    """Return a match finding, or ``None`` when nothing matched.

    The scan is best effort. A traversal limit or a value the detector cannot
    traverse is not a match: the value goes through, and the skip is counted
    by surface and reason. Everything after a limit goes unscanned.
    """
    finding = find_credential_material(value, policy, purpose="admission")
    if finding is None:
        return None
    if is_credential_match(finding):
        return finding
    record_credential_scan_skipped(surface=surface, reason=credential_scan_skip_reason(finding))
    return None

"""Metadata for the adapter facade method-budget rule."""

from __future__ import annotations

from elspeth_lints.core.protocols import Category, RuleMetadata, RuleScope, Severity

RULE_ID = "contract_invariants.adapter_method_budget"
GROWTH_SUGGESTION = (
    "If a new method is genuinely needed, consider whether the caller should inject the specific repository directly instead."
)
SLACK_SUGGESTION = (
    "Lower RATCHET in elspeth-lints/src/elspeth_lints/rules/contract_invariants/adapter_method_budget/rule.py "
    "to the current method count so the reduction is locked in."
)
FAIL_CLOSED_SUGGESTION = (
    "The adapter method budget could not be verified. Restore src/elspeth/core/landscape/plugin_audit_writer.py "
    "and its PluginAuditWriterAdapter class, or update the rule if the adapter has deliberately moved."
)

RULE_METADATA = RuleMetadata(
    id=RULE_ID,
    name="Adapter facade method budget",
    description=(
        "Ratchets the public method count of PluginAuditWriterAdapter so the adapter cannot grow back into a facade: "
        "growth fails, and slack fails until the ratchet is lowered."
    ),
    severity=Severity.ERROR,
    category=Category.CONTRACT_INVARIANTS,
    cwe=("CWE-710",),
    scope=RuleScope.WHOLE_REPO,
    path_filter=r"(^|/)core/landscape/plugin_audit_writer\.py$",
    examples_violation_count=3,
    examples_clean_count=1,
)

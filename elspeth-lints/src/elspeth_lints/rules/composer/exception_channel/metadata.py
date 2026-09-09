"""Metadata for the composer exception-channel rule."""

from __future__ import annotations

from elspeth_lints.core.protocols import Category, RuleMetadata, RuleScope, Severity

RULE_ID = "composer.exception_channel"
LEGACY_RULE_ID = "CEC1"
SUGGESTION = "raise ToolArgumentError(argument=..., expected=..., actual_type=...) from exc, or catch locally and return _failure_result"

RULE_METADATA = RuleMetadata(
    id=RULE_ID,
    name="Composer exception channel",
    description="Composer tool handlers must use ToolArgumentError for LLM-argument failures; a bare TypeError/ValueError may not escape a tool plane module.",
    severity=Severity.ERROR,
    category=Category.COMPOSER,
    cwe=("CWE-754", "CWE-396"),
    scope=RuleScope.INCREMENTAL,
    # The tool planes are a package (0b7c87dc1). ``_dispatch.py`` is the
    # dispatcher, not a handler: its bare raises are Tier-1 invariants
    # (catalog/snapshot identity) that MUST crash per the ToolArgumentError
    # contract, so it is excluded by name rather than allowlisted.
    path_filter=r"^web/composer/tools/(?!_dispatch\.py$).*\.py$",
    examples_violation_count=2,
    examples_clean_count=3,
)

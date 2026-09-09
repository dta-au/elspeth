"""Shared fixture harness coverage for built-in elspeth-lints rules."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from elspeth_lints.core.fixture_harness import (
    RuleFixtureCase,
    _load_fixture_rule,
    assert_fixture_case,
    discover_registry_fixture_cases,
    find_fixture_inventory_errors,
)
from elspeth_lints.core.registry import RuleRegistry

REGISTRY = RuleRegistry()
REGISTRY.load_builtin_rules()
BUILTIN_RULES = [rule for _rule_id, rule in REGISTRY.items()]
FIXTURE_CASES = discover_registry_fixture_cases(REGISTRY)


def test_all_builtin_rules_declare_fixtures() -> None:
    assert find_fixture_inventory_errors(BUILTIN_RULES) == []


def test_fixture_module_missing_rule_has_actionable_diagnostic(tmp_path: Path) -> None:
    fixture_rule = tmp_path / "_fixture_rule.py"
    fixture_rule.write_text("VALUE = 1\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="must expose a Rule-compatible RULE object"):
        _load_fixture_rule(fixture_rule)


@pytest.mark.parametrize("case", FIXTURE_CASES, ids=[case.name for case in FIXTURE_CASES])
@pytest.mark.skipif(
    sys.version_info[:2] != (3, 13),
    reason="elspeth-lints fixture fingerprints are version-specific; Python 3.13 is the canonical lint runtime",
)
def test_builtin_rule_fixture(case: RuleFixtureCase) -> None:
    assert_fixture_case(case)

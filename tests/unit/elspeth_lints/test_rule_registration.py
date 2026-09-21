"""The shipped analyzer has one rule-registration authority."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from elspeth_lints.core.registry import RuleRegistry
from elspeth_lints.rules import BUILTIN_RULES


def test_package_does_not_advertise_an_unused_rule_extension_group() -> None:
    """An entry-point declaration must not promise rules the CLI never loads."""
    root = Path(__file__).resolve().parents[3]
    package = tomllib.loads((root / "elspeth-lints/pyproject.toml").read_text(encoding="utf-8"))

    assert "elspeth_lints.rules" not in package["project"].get("entry-points", {})
    assert package["project"]["scripts"]["elspeth-lints"] == "elspeth_lints.core.cli:main"


def test_builtin_registry_loads_every_declared_rule_idempotently() -> None:
    """Every authored builtin is selectable and remains the same rule object."""
    registry = RuleRegistry()
    assert registry.ids() == ()

    registry.load_builtin_rules()
    registry.load_builtin_rules()

    assert registry.ids() == tuple(sorted(rule.id for rule in BUILTIN_RULES))
    for rule in BUILTIN_RULES:
        assert registry.get(rule.id) is rule


def test_registry_has_no_dormant_entry_point_loader() -> None:
    """Do not preserve a second public registration path disconnected from CLI."""
    root = Path(__file__).resolve().parents[3]
    source = root / "elspeth-lints/src/elspeth_lints/core/registry.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    registry = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "RuleRegistry")
    methods = {node.name for node in registry.body if isinstance(node, ast.FunctionDef)}

    assert "load_builtin_rules" in methods
    assert "load_entry_points" not in methods

"""Installed lint contracts must be independent of candidate checkout layout."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from elspeth_lints.rules.trust_boundary import shared

CONTRACTS = (
    ("elspeth.contracts", "src/elspeth/contracts/__init__.py"),
    ("elspeth.contracts.trust_boundary", "src/elspeth/contracts/trust_boundary.py"),
)


def _owned_exports(relative_path: str) -> tuple[str, ...]:
    """Use the owned literal source as the parity oracle; never import it."""
    root = Path(__file__).resolve().parents[3]
    tree = ast.parse((root / relative_path).read_text(encoding="utf-8"))
    assignments = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
    ]
    assert len(assignments) == 1
    exports = ast.literal_eval(assignments[0])
    assert isinstance(exports, list) and all(isinstance(name, str) for name in exports)
    return tuple(exports)


@pytest.mark.parametrize(("module", "relative_path"), CONTRACTS)
def test_installed_export_contract_does_not_require_checkout_ancestors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: str, relative_path: str
) -> None:
    expected = _owned_exports(relative_path)
    assert shared._canonical_star_exports(module) == expected
    installed_module = tmp_path / "release/lib/python3.13/site-packages/elspeth_lints/rules/trust_boundary/shared.py"
    installed_module.parent.mkdir(parents=True)
    installed_module.write_text("# synthetic installed module location\n", encoding="utf-8")
    monkeypatch.setattr(shared, "__file__", str(installed_module))

    assert shared._canonical_star_exports(module) == expected


@pytest.mark.parametrize(("module", "relative_path"), CONTRACTS)
def test_candidate_lookalike_contract_cannot_replace_installed_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: str, relative_path: str
) -> None:
    expected = _owned_exports(relative_path)
    assert "candidate_canary_export" not in expected
    candidate = tmp_path / "candidate"
    candidate_module = candidate / "elspeth-lints/src/elspeth_lints/rules/trust_boundary/shared.py"
    candidate_module.parent.mkdir(parents=True)
    candidate_module.write_text("# mutable lookalike installation location\n", encoding="utf-8")
    lookalike_contract = candidate / relative_path
    lookalike_contract.parent.mkdir(parents=True)
    lookalike_contract.write_text('__all__ = ["candidate_canary_export"]\n', encoding="utf-8")
    tree = ast.parse(lookalike_contract.read_text())
    assert isinstance(tree.body[0], ast.Assign)
    assert ast.literal_eval(tree.body[0].value) == ["candidate_canary_export"]
    monkeypatch.setattr(shared, "__file__", str(candidate_module))

    assert shared._canonical_star_exports(module) == expected

"""Exact reviewed publication guard shapes; this test grants no authority."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from elspeth_lints.core.ast_dump import stable_ast_dump


def _guard_recipe(source: str) -> tuple[str, ...]:
    methods = {node.name: node for node in ast.walk(ast.parse(source)) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    settlement = methods["_settle_pipeline_composition_proposal"]
    sync = next(node for node in settlement.body if isinstance(node, ast.FunctionDef) and node.name == "_sync")
    selected: list[ast.AST] = []
    for node in ast.walk(sync):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "enter_context"
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "_session_composer_mutation_transaction"
                for call in ast.walk(node)
            )
        ):
            selected.append(node)
        if isinstance(node, ast.If) and any(isinstance(attr, ast.Attribute) and attr.attr == "rowcount" for attr in ast.walk(node.test)):
            selected.append(node)
    for name in ("_record_composer_revocation_on_connection", "_record_required_composer_revocation_sync"):
        for node in ast.walk(methods[name]):
            if isinstance(node, ast.If):
                selected.append(node)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id
                in {
                    "prove_revocation_composer_binding_on_connection",
                    "require_composer_settlement_actor_on_connection",
                    "_verify_pipeline_lifecycle_authority",
                }
            ):
                selected.append(node)
    return tuple(hashlib.sha256(stable_ast_dump(node).encode()).hexdigest() for node in selected)


_EXPECTED_GUARD_RECIPE = (
    "5435f8f5f8712e3f4be36b6293ef82728ffab48e9d0cec7b91bc428d3e6addcc",
    "3fd7c51298cb2a046add7e7dfb56b64a95b8d3799c91fec2f793892d6bb26de8",
    "3fd7c51298cb2a046add7e7dfb56b64a95b8d3799c91fec2f793892d6bb26de8",
    "f03624c2aeded45b128ef08eec02cb352182f6766f615ff096aa5feab8281899",
    "a49497589bbd1c2563962f7f533903bf6a7e85d2ae705b9eb14c5292957ef033",
    "b9d7e881848cd6a53f0b2ac8e5c203d52eca81fb379142d93688bfdb0de9378d",
    "77fca6c6c2102673510838bc6a985043df4b8476ebd9b2128d405a27a29a4d76",
    "0f4d5dd4dbb52540842046e158e5f1691a37192e9392e3e7b87b01eb20ce8b27",
    "3b6a8e2854d8a603f4da43ede4297d9a0fe32da4e205bef9e5738e5625518242",
    "b983cc1f67b48ac5d519e9edca8bd600205029a59547a557d7a4d11f75b1d932",
    "70fcd8b7ff0d422a51f08fdf0d9d84706c8762ebcd00802b66591104dfb081b7",
    "0504cdf0ad9bb158cde06250d5ed470b5f63239029e31be126abfa51bf666b5b",
    "595609bce1e6259695f427418ae9b66b1d70c210fc342833588250b9ac14bc29",
    "3dc9dd2565447e76c5e72e745e03b2e6106766cc47fb805326d171b6cf7da197",
    "4de55f094d091581901a91125362d02a57fb9eb7bcb14f8dec70fa267b27c1f3",
    "2958e96295da4962d6da96264fa4b192c7e4d77bacbcc276eebf5aea23f7fea2",
)


def test_approved_pipeline_publication_guards_match_reviewed_source() -> None:
    root = Path(__file__).resolve().parents[3]
    source = (root / "src/elspeth/web/sessions/service.py").read_text()
    assert _guard_recipe(source) == _EXPECTED_GUARD_RECIPE


def test_approved_pipeline_publication_guard_mutations_are_refused() -> None:
    root = Path(__file__).resolve().parents[3]
    source = (root / "src/elspeth/web/sessions/service.py").read_text()
    assert _guard_recipe(source) == _EXPECTED_GUARD_RECIPE
    mutations = [
        ("_session_composer_mutation_transaction", "removed_positive_mutation_guard"),
        ("prove_revocation_composer_binding_on_connection", "removed_sealed_binding_guard"),
        ("require_composer_settlement_actor_on_connection", "removed_actor_guard"),
        ("if settled.rowcount != 1:", "if settled.rowcount != 2:"),
        ('current_trust_mode != "explicit_approve"', 'current_trust_mode == "explicit_approve"'),
        ("if current != eligibility.authority:", "if current == eligibility.authority:"),
    ]
    for original, replacement in mutations:
        assert original in source
        assert _guard_recipe(source.replace(original, replacement)) != _EXPECTED_GUARD_RECIPE

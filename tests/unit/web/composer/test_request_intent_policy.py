"""Natural-language request interpretation belongs to the provider."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.helpers.tree_gate import iter_gate_sources

_RETIRED_REQUEST_HEURISTICS = frozenset(
    {
        "PipelineMutationIntentDecision",
        "classify_pipeline_mutation_intent",
        "is_referential_pipeline_mutation_intent",
        "last_failure_was_pre_state_interpretation_review",
        "pre_state_interpretation_review_repair_message",
    }
)


def _retired_request_heuristics(source: str) -> set[str]:
    """Find definitions and imports even when a consumer aliases them."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            found.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
    return found & _RETIRED_REQUEST_HEURISTICS


@pytest.mark.parametrize("name", sorted(_RETIRED_REQUEST_HEURISTICS))
def test_retired_request_heuristic_guard_detects_definition_and_aliased_import(name: str) -> None:
    assert _retired_request_heuristics(f"def {name}(): pass") == {name}
    assert _retired_request_heuristics(f"from policy import {name} as renamed") == {name}
    assert _retired_request_heuristics(f"policy.{name}()") == {name}


def test_retired_request_heuristic_guard_ignores_model_text_and_unrelated_policy() -> None:
    assert _retired_request_heuristics('content = "classify_pipeline_mutation_intent"\nstate_is_structurally_empty(state)') == set()


def test_composer_does_not_reintroduce_closed_request_grammar() -> None:
    """Route on declared state; let provider tool calls interpret the request.

    Behavioral routing and tool-error recovery are covered by service tests.
    This guard prevents the retired closed English grammar and error-text
    recognizer from being restored as shared policy helpers.
    """
    composer_root = Path(__file__).resolve().parents[4] / "src/elspeth/web/composer"
    sources = tuple(iter_gate_sources(composer_root))
    assert any(parsed.path.name == "service.py" for parsed in sources)
    violations = {
        parsed.path.relative_to(composer_root).as_posix(): sorted(found)
        for parsed in sources
        if (found := _retired_request_heuristics(parsed.source))
    }
    assert violations == {}

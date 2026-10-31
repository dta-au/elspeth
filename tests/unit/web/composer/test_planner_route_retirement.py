"""The empty-state planner route and its escape hatch stay retired (U-30, H2).

Production authors every request through the compose loop. ``plan_pipeline``,
its hatch, the planner-only request builders, the planner capability manifest
and the two planner boot-probe surfaces were deleted rather than kept for a
hypothetical caller. This scan fails if any of them is defined or referenced
again anywhere under ``src/elspeth``. The live names the planner module still
exports (the plan result, the planner error, the continuation budget and the
conversation-context carriers) are outside the retired set.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[4] / "src" / "elspeth"

RETIRED_NAMES = frozenset(
    {
        "plan_pipeline",
        "_complete_planner_provider",
        "PlannerModelConfig",
        "planner_tool_definitions",
        "planner_terminal_tool_definition",
        "build_planner_request_kwargs",
        "build_planner_capability_manifest",
        "PlannerCapabilityManifest",
        "PLANNER_IMPLEMENTATION_ID",
        "stamp_planner_terminal",
        "PlannerTerminalContract",
        "canonical_planner_terminal_contract",
    }
)
RETIRED_STRINGS = frozenset(
    {
        "planner_tools",
        "hatch_terminal",
        "elspeth.web.composer.pipeline_planner.plan_pipeline",
    }
)


def retired_references(tree: ast.AST, names: frozenset[str], strings: frozenset[str]) -> list[str]:
    """Every definition of, reference to, import of, or literal naming a retired item."""
    hits: list[str] = []
    for node in ast.walk(tree):
        found: str | None = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names:
            found = node.name
        elif isinstance(node, ast.Name) and node.id in names:
            found = node.id
        elif isinstance(node, ast.Attribute) and node.attr in names:
            found = node.attr
        elif isinstance(node, ast.alias) and (node.name.split(".")[-1] in names or node.asname in names):
            found = node.name
        elif isinstance(node, ast.Constant) and type(node.value) is str and node.value in strings:
            found = node.value
        if found is not None:
            hits.append(f"{node.lineno}: {found}")
    return hits


@cache
def _source_trees() -> tuple[tuple[str, ast.AST], ...]:
    return tuple((str(path.relative_to(_SRC)), ast.parse(path.read_text(encoding="utf-8"))) for path in sorted(_SRC.rglob("*.py")))


def test_no_source_defines_or_references_the_retired_planner_route() -> None:
    trees = _source_trees()
    assert len(trees) > 400, "the scan must cover the whole source tree"
    offenders = {name: hits for name, tree in trees if (hits := retired_references(tree, RETIRED_NAMES, RETIRED_STRINGS))}
    assert offenders == {}


def test_the_finder_sees_a_live_planner_export() -> None:
    # Positive control on the real tree: the same finder, asked for a name that
    # must still exist, finds its definition and its production importers.
    hits = [name for name, tree in _source_trees() if retired_references(tree, frozenset({"PipelinePlanResult"}), frozenset())]
    assert "web/composer/pipeline_planner.py" in hits
    assert len(hits) > 2


@pytest.mark.parametrize("name", sorted(RETIRED_NAMES))
@pytest.mark.parametrize(
    "template",
    [
        "from elspeth.web.composer.pipeline_planner import {name}\n",
        "from elspeth.web.composer.pipeline_planner import {name} as another_name\n",
        "def {name}():\n    return None\n",
        "import elspeth.web.composer.pipeline_planner as planner\nplanner.{name}()\n",
    ],
)
def test_re_adding_a_retired_name_is_caught(name: str, template: str) -> None:
    assert retired_references(ast.parse(template.format(name=name)), RETIRED_NAMES, RETIRED_STRINGS)


@pytest.mark.parametrize("value", sorted(RETIRED_STRINGS))
def test_re_adding_a_retired_surface_literal_is_caught(value: str) -> None:
    assert retired_references(ast.parse(f"SURFACE = {value!r}\n"), RETIRED_NAMES, RETIRED_STRINGS)


def test_private_planning_application_stays_absent() -> None:
    assert not (_SRC / "web" / "composer" / "planning_application.py").exists()


def test_unrelated_aliased_import_is_not_retired() -> None:
    assert retired_references(ast.parse("from somewhere import live as another_name\n"), RETIRED_NAMES, RETIRED_STRINGS) == []

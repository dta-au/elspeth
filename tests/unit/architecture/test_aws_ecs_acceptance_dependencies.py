"""Dependency-direction guard for the AWS ECS acceptance extraction."""

from __future__ import annotations

import ast
import importlib.util
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WEB_ROOT = REPO_ROOT / "src" / "elspeth" / "web"
FACADE = WEB_ROOT / "aws_ecs_acceptance.py"
PRIVATE_ROOT = WEB_ROOT / "_aws_ecs_acceptance"
PRIVATE_PACKAGE = "elspeth.web._aws_ecs_acceptance"
FACADE_MODULE = "elspeth.web.aws_ecs_acceptance"

LAYERS = {
    "contracts": 0,
    "secure_documents": 1,
    "state": 1,
    "http_client": 1,
    "receipt_contracts": 1,
    "capture": 2,
    "ecs_metadata": 2,
    "s3": 2,
    "textract": 2,
    "bedrock": 2,
    "operator_telemetry": 2,
    "manifest_schema": 2,
    "scenario_inventory": 2,
    "gate_ledger": 2,
    "manifest": 3,
    "task_definition": 3,
    "orphan_sweep": 3,
    "receipt_store": 3,
    "approvals": 3,
    "evidence": 3,
    "cleanup": 4,
    "control_service": 4,
}

FORBIDDEN_EDGES = {
    ("s3", "bedrock"),
    ("s3", "operator_telemetry"),
    ("s3", "textract"),
    ("bedrock", "s3"),
    ("bedrock", "operator_telemetry"),
    ("bedrock", "textract"),
    ("operator_telemetry", "s3"),
    ("operator_telemetry", "bedrock"),
    ("operator_telemetry", "textract"),
    ("textract", "s3"),
    ("textract", "bedrock"),
    ("textract", "operator_telemetry"),
    ("manifest_schema", "manifest"),
    ("manifest_schema", "task_definition"),
    ("manifest_schema", "orphan_sweep"),
    ("manifest_schema", "receipt_store"),
    ("manifest_schema", "approvals"),
    ("manifest_schema", "evidence"),
    ("manifest_schema", "cleanup"),
    ("manifest_schema", "control_service"),
    ("scenario_inventory", "manifest"),
    ("scenario_inventory", "task_definition"),
    ("scenario_inventory", "orphan_sweep"),
    ("scenario_inventory", "receipt_store"),
    ("scenario_inventory", "approvals"),
    ("scenario_inventory", "evidence"),
    ("scenario_inventory", "cleanup"),
    ("scenario_inventory", "control_service"),
    ("receipt_store", "manifest"),
    ("receipt_store", "control_service"),
    ("gate_ledger", "evidence"),
    ("gate_ledger", "cleanup"),
    ("gate_ledger", "control_service"),
    ("cleanup", "control_service"),
    ("control_service", "cleanup"),
}


def _absolute_import_base(source_module: str, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = source_module.rpartition(".")[0]
    relative_name = f"{'.' * node.level}{node.module or ''}"
    return importlib.util.resolve_name(relative_name, package)


def _imported_modules(source_module: str, source: str) -> set[str]:
    """Return fully qualified imported module candidates from *source*."""

    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_import_base(source_module, node)
            imported.add(base)
            # ``from package import module`` carries the module in the alias,
            # while ``from package.module import name`` carries it in ``base``.
            imported.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return imported


def _private_dependencies(source_module: str, source: str) -> tuple[set[str], set[str]]:
    """Return known and unknown direct private-package module dependencies."""

    known: set[str] = set()
    unknown: set[str] = set()
    prefix = f"{PRIVATE_PACKAGE}."
    for imported in _imported_modules(source_module, source):
        if not imported.startswith(prefix):
            continue
        private_name = imported.removeprefix(prefix).split(".", maxsplit=1)[0]
        if private_name in LAYERS:
            known.add(private_name)
        else:
            unknown.add(private_name)
    return known, unknown


def _imports_facade(source_module: str, source: str) -> bool:
    imported = _imported_modules(source_module, source)
    return FACADE_MODULE in imported


def _assert_acyclic(graph: Mapping[str, Iterable[str]]) -> None:
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(module: str) -> None:
        if module in visiting:
            cycle_start = visiting.index(module)
            cycle = [*visiting[cycle_start:], module]
            pytest.fail(f"private AWS ECS acceptance dependency cycle: {' -> '.join(cycle)}")
        if module in visited:
            return
        visiting.append(module)
        for dependency in sorted(graph.get(module, ())):
            visit(dependency)
        visiting.pop()
        visited.add(module)

    for module in sorted(graph):
        visit(module)


def test_relative_import_resolution_is_fully_qualified() -> None:
    source = """
from . import contracts
from .state import AcceptanceState
from elspeth.web._aws_ecs_acceptance.http_client import AcceptanceHttpClient
from elspeth.web.operator_telemetry import bootstrap_operator_telemetry
"""

    known, unknown = _private_dependencies(f"{PRIVATE_PACKAGE}.capture", source)

    assert known == {"contracts", "state", "http_client"}
    assert unknown == set()
    assert not _imports_facade(f"{PRIVATE_PACKAGE}.capture", source)


@pytest.mark.parametrize(
    ("graph", "expected_cycle"),
    [
        ({"s3": {"bedrock"}, "bedrock": {"s3"}}, r"bedrock -> s3 -> bedrock"),
        ({"capture": {"capture"}}, r"capture -> capture"),
    ],
)
def test_cycle_detection_includes_same_layer_and_self_cycles(
    graph: Mapping[str, Iterable[str]],
    expected_cycle: str,
) -> None:
    with pytest.raises(pytest.fail.Exception, match=expected_cycle):
        _assert_acyclic(graph)


def test_aws_ecs_acceptance_private_dependencies_obey_layers() -> None:
    facade_source = FACADE.read_text(encoding="utf-8")
    facade_dependencies, facade_unknown = _private_dependencies(FACADE_MODULE, facade_source)
    assert not facade_unknown

    if not PRIVATE_ROOT.exists():
        assert not facade_dependencies
        return

    init_path = PRIVATE_ROOT / "__init__.py"
    init_tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
    assert all(
        isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) for node in init_tree.body
    )

    module_paths = {path.stem: path for path in PRIVATE_ROOT.glob("*.py") if path.name != "__init__.py"}
    assert set(module_paths) == set(LAYERS), (
        f"private module set mismatch: missing={sorted(set(LAYERS) - set(module_paths))} unlisted={sorted(set(module_paths) - set(LAYERS))}"
    )

    graph: dict[str, set[str]] = {}
    for module, path in sorted(module_paths.items()):
        source = path.read_text(encoding="utf-8")
        source_module = f"{PRIVATE_PACKAGE}.{module}"
        dependencies, unknown = _private_dependencies(source_module, source)
        assert not unknown, f"{module} imports unlisted private modules: {sorted(unknown)}"
        assert not _imports_facade(source_module, source), f"{module} imports the public facade"
        graph[module] = dependencies

        upward = {dependency for dependency in graph[module] if LAYERS[dependency] > LAYERS[module]}
        assert not upward, f"{module} has upward dependencies: {sorted(upward)}"
        forbidden = {(module, dependency) for dependency in graph[module]} & FORBIDDEN_EDGES
        assert not forbidden, f"forbidden private dependencies: {sorted(forbidden)}"

    _assert_acyclic(graph)

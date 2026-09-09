"""Dependency-direction guard for the Azure Container Apps acceptance package.

``_azure_container_apps_acceptance`` builds on ``_acceptance_common`` only: it
never imports ``_aws_ecs_acceptance``, either facade, or the web app, and its
three modules obey one downward layering (``receipt_contracts`` <
``controller`` < ``evidence``). The ECS package is the reference, never a
dependency (plan §8.2, §14: the shared core stays the only shared thing).
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WEB_ROOT = REPO_ROOT / "src" / "elspeth" / "web"
FACADE = WEB_ROOT / "azure_container_apps_acceptance.py"
PRIVATE_ROOT = WEB_ROOT / "_azure_container_apps_acceptance"
PRIVATE_PACKAGE = "elspeth.web._azure_container_apps_acceptance"
FACADE_MODULE = "elspeth.web.azure_container_apps_acceptance"

LAYERS = {"receipt_contracts": 0, "controller": 1, "evidence": 2}

FORBIDDEN_PREFIXES = (
    "elspeth.web._aws_ecs_acceptance",
    "elspeth.web.aws_ecs_acceptance",
    "elspeth.web.app",
    "elspeth.web.readiness",
    "elspeth.web.config",
    "elspeth.web.sessions.service",
    "azure",
    "boto3",
    "botocore",
)


def _imported_modules(source_module: str, source: str) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = (
                node.module or ""
                if node.level == 0
                else importlib.util.resolve_name(f"{'.' * node.level}{node.module or ''}", source_module.rpartition(".")[0])
            )
            imported.add(base)
            imported.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return imported


def _private_dependencies(source_module: str, source: str) -> set[str]:
    prefix = f"{PRIVATE_PACKAGE}."
    return {
        imported.removeprefix(prefix).split(".", maxsplit=1)[0]
        for imported in _imported_modules(source_module, source)
        if imported.startswith(prefix)
    }


def test_package_module_set_and_init_are_the_declared_ones() -> None:
    modules = {path.stem for path in PRIVATE_ROOT.glob("*.py") if path.name != "__init__.py"}
    assert modules == set(LAYERS)
    init_tree = ast.parse((PRIVATE_ROOT / "__init__.py").read_text(encoding="utf-8"))
    assert all(
        isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) for node in init_tree.body
    )
    assert (PRIVATE_ROOT / "README.md").exists()


def test_package_and_facade_never_import_ecs_the_app_or_a_provider_sdk() -> None:
    for path in (FACADE, *sorted(PRIVATE_ROOT.glob("*.py"))):
        module = FACADE_MODULE if path == FACADE else f"{PRIVATE_PACKAGE}.{path.stem}"
        offending = {name for name in _imported_modules(module, path.read_text(encoding="utf-8")) if name.startswith(FORBIDDEN_PREFIXES)}
        assert not offending, f"{path.name} imports {sorted(offending)}"


def test_private_modules_obey_the_layering_and_never_import_the_facade() -> None:
    for module, layer in LAYERS.items():
        source = (PRIVATE_ROOT / f"{module}.py").read_text(encoding="utf-8")
        source_module = f"{PRIVATE_PACKAGE}.{module}"
        imported = _imported_modules(source_module, source)
        assert FACADE_MODULE not in imported, f"{module} imports the public facade"
        dependencies = _private_dependencies(source_module, source) - {module}
        assert dependencies <= set(LAYERS), f"{module} imports unlisted private modules: {sorted(dependencies - set(LAYERS))}"
        upward = {dependency for dependency in dependencies if LAYERS[dependency] >= layer}
        assert not upward, f"{module} has upward or same-layer dependencies: {sorted(upward)}"


def test_the_facade_only_reaches_the_shared_core_and_its_own_package() -> None:
    imported = _imported_modules(FACADE_MODULE, FACADE.read_text(encoding="utf-8"))
    elspeth_imports = {name for name in imported if name.startswith("elspeth.")}
    assert elspeth_imports, "the facade must bind the package"
    assert all(name.startswith(("elspeth.web._acceptance_common", PRIVATE_PACKAGE)) for name in elspeth_imports), sorted(elspeth_imports)

"""Dependency direction for the shared acceptance core.

``_acceptance_common`` is what both provider packages build on: it may import
``elspeth.contracts``, ``elspeth.core`` and the web schema constants, but never
a provider package or a facade. The ECS package's own layering gate
(``tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py``) stays
byte-unedited by the extraction and keeps holding.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
COMMON_ROOT = REPO_ROOT / "src" / "elspeth" / "web" / "_acceptance_common"
FORBIDDEN_PREFIXES = (
    "elspeth.web._aws_ecs_acceptance",
    "elspeth.web.aws_ecs_acceptance",
    "elspeth.web._azure_container_apps_acceptance",
    "elspeth.web.azure_container_apps_acceptance",
    "elspeth.web.app",
)
EXPECTED_MODULES = {
    "compatibility_gate",
    "errors",
    "http_client",
    "identity",
    "receipt_validation",
    "replica_probes",
    "schema_facts",
    "secure_documents",
}


def _imports(path: Path) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            imported.add(node.module)
    return imported


def test_shared_core_module_set_is_the_declared_one() -> None:
    modules = {path.stem for path in COMMON_ROOT.glob("*.py") if path.name != "__init__.py"}
    assert modules == EXPECTED_MODULES


def test_shared_core_never_imports_a_provider_package_or_facade() -> None:
    for path in sorted(COMMON_ROOT.glob("*.py")):
        offending = {name for name in _imports(path) if name.startswith(FORBIDDEN_PREFIXES)}
        assert not offending, f"{path.name} imports {sorted(offending)}"


def test_shared_core_init_is_documentation_only() -> None:
    tree = ast.parse((COMMON_ROOT / "__init__.py").read_text(encoding="utf-8"))
    assert all(
        isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) for node in tree.body
    )

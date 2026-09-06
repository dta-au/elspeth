"""``ComposerPreferencesTransition.prior`` has no consumer (elspeth-d336060892).

``PreferencesService.update_composer_preferences`` reads the prior row inside
the same transaction as its upsert. On SQLite that read is serialised
(``create_session_engine`` opens the transaction with ``BEGIN IMMEDIATE``);
on PostgreSQL it is a READ COMMITTED snapshot a concurrent PATCH for the same
user can invalidate, so ``prior`` must not be promoted into a Landscape or
audit emit until a user-keyed serialising lock exists (remedy (b) in the
ticket). ``PriorPreferencesSnapshot.serialised`` carries the fact; this
module is the check that nothing acts on ``prior`` today:

- no module in the preferences package reads a ``.prior`` attribute;
- the route reads only ``transition.current``;
- the preferences package imports nothing from the Landscape;
- the only modules in the web tree that reach the preferences service are the
  three named here, and none of them reads ``.prior``.

Every inventory is exact (an equality against a closed set), so a new consumer
fails the test rather than widening it silently. Each negative measurement is
paired with a positive control on a synthetic source so a collector that finds
nothing is known to be able to find something.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.helpers.tree_gate import iter_gate_sources

_REPO_ROOT = Path(__file__).resolve().parents[4]
_WEB_ROOT = _REPO_ROOT / "src" / "elspeth" / "web"
_PREFERENCES_PACKAGE = _WEB_ROOT / "preferences"
_ROUTES = _PREFERENCES_PACKAGE / "routes.py"

# Modules that reach ``app.state.preferences_service``: the constructor site,
# the PATCH/GET route, and the tutorial's read of the current mode.
_PREFERENCES_SERVICE_CONSUMERS = frozenset(
    {
        "app.py",
        "preferences/routes.py",
        "composer/tutorial_service.py",
    }
)


def _attribute_sites(tree: ast.AST, attr: str) -> list[tuple[int, str]]:
    """Every ``<expr>.<attr>`` read in ``tree`` as ``(line, source)``."""
    return sorted((node.lineno, ast.unparse(node)) for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == attr)


def _landscape_imports(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names if "landscape" in alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and "landscape" in node.module:
            found.append(node.module)
    return sorted(found)


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def test_positive_controls_find_what_the_inventories_look_for() -> None:
    synthetic = ast.parse(
        "from elspeth.core.landscape.recorder import LandscapeRecorder\n"
        "transition = await service.update_composer_preferences(user_id, body)\n"
        "recorder.record(transition.prior.value)\n"
    )
    assert _attribute_sites(synthetic, "prior") == [(3, "transition.prior")]
    assert _landscape_imports(synthetic) == ["elspeth.core.landscape.recorder"]


def test_no_module_in_the_preferences_package_reads_prior() -> None:
    sites = {
        _relative(parsed.path, _PREFERENCES_PACKAGE): _attribute_sites(parsed.tree, "prior")
        for parsed in iter_gate_sources(_PREFERENCES_PACKAGE)
    }
    consumers = {path: found for path, found in sites.items() if found}
    assert "routes.py" in sites and "service.py" in sites, sorted(sites)
    assert consumers == {}, (
        "a consumer of ComposerPreferencesTransition.prior appeared; on PostgreSQL that value is a "
        "READ COMMITTED snapshot, so a user-keyed serialising lock must land first (elspeth-d336060892)"
    )


def test_route_consumer_reads_only_current_from_the_transition() -> None:
    tree = ast.parse(_ROUTES.read_text(encoding="utf-8"))
    bindings = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "transition" for target in node.targets)
    ]
    assert len(bindings) == 1
    assert isinstance(bindings[0].value, ast.Await)
    reads = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "transition"
    }
    assert reads == {"current"}


def test_preferences_package_imports_nothing_from_the_landscape() -> None:
    imports = {
        _relative(parsed.path, _PREFERENCES_PACKAGE): _landscape_imports(parsed.tree) for parsed in iter_gate_sources(_PREFERENCES_PACKAGE)
    }
    assert {path: found for path, found in imports.items() if found} == {}


def test_only_the_named_web_modules_reach_the_preferences_service_and_none_reads_prior() -> None:
    reaching: dict[str, list[tuple[int, str]]] = {}
    for parsed in iter_gate_sources(_WEB_ROOT):
        if not _attribute_sites(parsed.tree, "preferences_service"):
            continue
        reaching[_relative(parsed.path, _WEB_ROOT)] = _attribute_sites(parsed.tree, "prior")
    assert set(reaching) == _PREFERENCES_SERVICE_CONSUMERS, sorted(reaching)
    assert {path: found for path, found in reaching.items() if found} == {}

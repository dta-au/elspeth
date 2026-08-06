from __future__ import annotations

import ast
from pathlib import Path

import pytest

# The composer-service surface spans every top-level module file in
# ``src/elspeth/web/composer/``. Module-tail patches of ComposerServiceImpl
# methods or ``_PHASE3_*`` sentinels are forbidden in any of them — the
# anti-pattern can hide in a sibling module just as easily as in
# ``service.py`` itself.
#
# Globbed discovery (rather than a hand-listed tuple) so the gate
# automatically extends to new sibling modules without a maintenance step.
# The prior hand-list silently missed every module added after its initial
# authoring (elspeth-59cdfcaf67) — globbing closes the lifecycle hole.
#
# Subdirectory packages (``composer/tools/``, ``composer/skills/``,
# ``composer/guided/``) are intentionally excluded: the forbidden pattern
# (``ComposerServiceImpl.<attr> = ...`` at module tail) is a top-level
# service-surface concern, and the subdirectories are tool implementations
# / skill graphs that never reference the service class identifier.

_REPO_ROOT = Path(__file__).resolve().parents[4]
_COMPOSER_DIR = _REPO_ROOT / "src" / "elspeth" / "web" / "composer"

# Sort for deterministic test-ID ordering across collections.
# Exclude ``__init__.py`` — it's a package marker, not a module-surface
# carrier, and the anti-pattern's only known incidence in __init__ would be
# an explicit re-export, not a module-tail patch.
COMPOSER_SERVICE_SURFACE_PATHS: tuple[Path, ...] = tuple(sorted(p for p in _COMPOSER_DIR.glob("*.py") if p.name != "__init__.py"))

# Module-load smoke assertions: catch a glob that suddenly returns nothing
# (path drift after a refactor) before the parametrized tests get a chance
# to silently report zero collected cases.
assert COMPOSER_SERVICE_SURFACE_PATHS, f"globbed surface paths must not be empty; checked {_COMPOSER_DIR}"
assert all(p.is_file() for p in COMPOSER_SERVICE_SURFACE_PATHS), "globbed surface paths must all be real files; check directory layout"


@pytest.mark.parametrize("module_path", COMPOSER_SERVICE_SURFACE_PATHS, ids=lambda p: p.name)
def test_composer_service_impl_not_patched_at_module_tail(module_path: Path) -> None:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    forbidden_assignments = {
        ("ComposerServiceImpl", "_run_one_turn_for_test"),
        ("ComposerServiceImpl", "_serialize_response_via_walker"),
        ("ComposerServiceImpl", "_state_payload_for_compose_turn_for_test"),
        ("ComposerServiceImpl", "__init__"),
    }
    hits: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and (target.value.id, target.attr) in forbidden_assignments
            ):
                hits.append(f"{target.value.id}.{target.attr}")
            if isinstance(target, ast.Name) and target.id.startswith("_PHASE3_"):
                hits.append(target.id)
    assert hits == [], f"forbidden module-tail patches in {module_path}: {hits}"


def test_authoring_surface_telemetry_emission_shape_is_pinned() -> None:
    """B4: the recall denominator's event name and field names are pinned.

    ``composer_authoring_surface_selected`` (with ``authoring_surface`` and
    ``intent_is_explicit_mutation``) is the ONLY denominator for any
    intent-classifier recall measurement, and it had zero test coverage —
    a rename or field drop would silently end the measurement mid-stream.
    This is a source-shape pin: it proves the emission still exists with
    these names, not that the path executes (the live journald events are
    the execution evidence, and they are first-turn-only —
    ``intent_is_explicit_mutation`` is None unless the state is empty).
    """
    source = (_COMPOSER_DIR / "service.py").read_text(encoding="utf-8")
    emission_start = source.index('"composer_authoring_surface_selected"')
    emission_window = source[emission_start : emission_start + 400]
    assert 'authoring_surface="planner" if planner_eligible else "compose_loop"' in emission_window
    assert "intent_is_explicit_mutation=intent_is_explicit_mutation" in emission_window
    assert "session_id=session_id" in emission_window

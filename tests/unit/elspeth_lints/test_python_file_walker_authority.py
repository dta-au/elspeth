"""Every Python-file walker in the repository shares ONE exclusion authority.

Nine walkers once solved worktree-blindness with four different idioms
(elspeth-faadf9873e). This module pins the consolidation two ways:

* :data:`WALKERS` runs every surviving walker over one synthetic tree and
  requires each to yield only the real source file. A NEW walker must be
  registered here — the gap this closes is that a new walker inherited
  nothing from the fix applied to its siblings.
* :func:`test_no_private_python_file_walk_outside_the_authority` scans the
  lint package and the CI scripts for a fresh ``rglob("*.py")`` /
  ``os.walk`` so a tenth walker cannot appear silently.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest
from scripts.cicd import runtime_rejection_parity
from tests.helpers import tree_gate
from tests.unit import test_mock_discipline_baseline

from elspeth_lints.core import ast_walker
from elspeth_lints.core.ast_walker import (
    EXCLUDED_WALK_DIRS,
    EXCLUDED_WALK_PREFIXES,
    ParsedPythonFile,
    iter_python_files,
    walk_python_files,
)
from elspeth_lints.rules.audit_evidence import shared as audit_evidence_shared
from elspeth_lints.rules.manifest.contract_manifest import rule as contract_manifest_rule
from elspeth_lints.rules.trust_tier.tier_model import rule as tier_model_rule

REPO_ROOT = Path(__file__).resolve().parents[3]

REAL_FILE = Path("src/elspeth/real.py")
LEAK_FILES: tuple[Path, ...] = (
    Path(".claude/worktrees/wt/src/elspeth/LEAK.py"),
    Path(".worktrees/y/src/elspeth/dotwt.py"),
    Path(".venv/lib/v.py"),
    Path("node_modules/x/n.py"),
)

# A minimal module every walker can parse and the contract-manifest scanner
# recognises as a registration site (canonical import + one-argument call).
_MODULE_SOURCE = "from elspeth.contracts.declaration_contracts import register_declaration_contract\nregister_declaration_contract(1)\n"


def _paths_of_parsed(results: Iterable[object]) -> Iterable[Path]:
    for parsed in results:
        assert isinstance(parsed, ParsedPythonFile), parsed
        yield parsed.path


def _contract_manifest_walk(root: Path) -> Iterable[Path]:
    calls = contract_manifest_rule.scan_source_tree(root, root / "__absent_manifest__.py")
    return (root / call.file_path for call in calls)


# Registry of every Python-file walker in the repository. Each entry maps a
# walker to a callable ``root -> iterable of absolute paths``. Adding a walker
# anywhere without registering it here is the defect this test exists to catch.
WALKERS: dict[str, Callable[[Path], Iterable[Path]]] = {
    "ast_walker.iter_python_files": iter_python_files,
    "ast_walker.walk_python_files": lambda root: _paths_of_parsed(walk_python_files(root)),
    "tier_model.iter_scannable_python_files": tier_model_rule.iter_scannable_python_files,
    "audit_evidence.shared.iter_python_paths": audit_evidence_shared.iter_python_paths,
    "contract_manifest.scan_source_tree": _contract_manifest_walk,
    "runtime_rejection_parity._iter_python_files": runtime_rejection_parity._iter_python_files,
    "test_mock_discipline_baseline._iter_python_files": test_mock_discipline_baseline._iter_python_files,
    "tree_gate.iter_gate_files": tree_gate.iter_gate_files,
    "tree_gate.iter_gate_sources": lambda root: _paths_of_parsed(tree_gate.iter_gate_sources(root)),
}


@pytest.fixture
def synthetic_tree(tmp_path: Path) -> Path:
    for relative in (REAL_FILE, *LEAK_FILES):
        target = tmp_path / relative
        target.parent.mkdir(parents=True)
        target.write_text(_MODULE_SOURCE, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("walker_name", sorted(WALKERS))
def test_every_walker_prunes_worktrees_and_dependency_trees(synthetic_tree: Path, walker_name: str) -> None:
    walker = WALKERS[walker_name]

    seen = sorted(Path(path).resolve().relative_to(synthetic_tree.resolve()) for path in walker(synthetic_tree))

    assert seen == [REAL_FILE], f"{walker_name} leaked {seen}"


def test_both_worktree_conventions_are_excluded_and_no_bare_worktrees_component() -> None:
    assert ".worktrees" in EXCLUDED_WALK_DIRS
    assert (".claude", "worktrees") in EXCLUDED_WALK_PREFIXES
    # A bare ``worktrees`` component would silently drop any tracked directory
    # of that name; the agent convention is pinned as a root-relative prefix.
    assert "worktrees" not in EXCLUDED_WALK_DIRS
    assert all(len(prefix) > 1 for prefix in EXCLUDED_WALK_PREFIXES)


_PRIVATE_WALK_RE = re.compile(r"""rglob\(\s*['"]\*\.py['"]\s*\)|\bos\.walk\(""")
_AUTHORITY = Path(ast_walker.__file__).resolve()
# ``tests/`` is pinned wholesale: every whole-tree gate walks through
# ``tests/helpers/tree_gate.py`` (the shared-checkout race fix, 2026-08-29),
# so a fresh ``rglob("*.py")`` anywhere under tests/ is a new unprotected gate.
_PINNED_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "elspeth-lints" / "src",
    REPO_ROOT / "scripts" / "cicd",
    REPO_ROOT / "tests",
)
_PINNED_FILES: tuple[Path, ...] = ()
# Walks that are NOT Python-file walks and so carry no exclusion obligation:
# ``tests/conftest.py`` fingerprints EVERY directory entry (lstat of each
# name, sorted) to detect stray writes into ``.elspeth/`` — pruning anything
# from it would defeat its purpose.
_NOT_PYTHON_FILE_WALKS: frozenset[Path] = frozenset({REPO_ROOT / "tests" / "conftest.py"})


def test_no_private_python_file_walk_outside_the_authority() -> None:
    candidates = [path for root in _PINNED_ROOTS for path in iter_python_files(root) if "fixtures" not in path.parts]
    candidates.extend(_PINNED_FILES)
    self_and_authority_helper = {Path(__file__).resolve(), Path(tree_gate.__file__).resolve(), *_NOT_PYTHON_FILE_WALKS}
    candidates = [path for path in candidates if path.resolve() not in self_and_authority_helper]
    assert candidates, "pin scanned nothing"

    offenders = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in candidates
        if path.resolve() != _AUTHORITY and _PRIVATE_WALK_RE.search(path.read_text(encoding="utf-8"))
    )

    assert offenders == [], f"private Python-file walks outside ast_walker: {offenders}"

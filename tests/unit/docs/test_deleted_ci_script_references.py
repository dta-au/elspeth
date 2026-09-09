"""Regression guards for deleted CI script references in active docs."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LINT_MIGRATION_STATUS = REPO_ROOT / "config" / "cicd" / "lint_migration_status.yaml"
HISTORICAL_DOC_PREFIXES = (
    "docs-archive/",
    "docs/plans/",
    "docs/specs/",
)
HISTORICAL_DOC_PATHS = ("CHANGELOG.md",)


def _deleted_migrated_scripts() -> set[str]:
    scripts: set[str] = set()
    text = LINT_MIGRATION_STATUS.read_text(encoding="utf-8")
    for block in text.split("\n  - old_script: ")[1:]:
        old_script = block.splitlines()[0].strip()
        if re.search(r"(?m)^    status: deleted$", block):
            scripts.add(old_script)
    return scripts


def _active_reference_paths() -> list[Path]:
    paths = [
        *REPO_ROOT.glob("*.md"),
        *REPO_ROOT.glob("docs/**/*.md"),
        # .agents/skills is the canonical skills tree; .claude/skills holds symlinks into it.
        *REPO_ROOT.glob(".agents/skills/**/*.md"),
        *REPO_ROOT.glob("src/elspeth/**/*.py"),
    ]
    return [
        path
        for path in sorted(paths)
        if not any(path.relative_to(REPO_ROOT).as_posix().startswith(prefix) for prefix in HISTORICAL_DOC_PREFIXES)
        and path.relative_to(REPO_ROOT).as_posix() not in HISTORICAL_DOC_PATHS
    ]


def test_active_docs_and_source_do_not_reference_deleted_migrated_ci_scripts() -> None:
    offenders: list[str] = []

    for path in _active_reference_paths():
        text = path.read_text(encoding="utf-8")
        rel_path = path.relative_to(REPO_ROOT).as_posix()
        for script in _deleted_migrated_scripts():
            for needle in (script, Path(script).name):
                if needle in text:
                    offenders.append(f"{rel_path}: {needle}")

    assert offenders == []

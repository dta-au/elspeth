"""Enumerate executable and parked workflow source without hiding duplicates."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EXECUTABLE_WORKFLOWS = ROOT / ".github/workflows"
PREPARED_WORKFLOWS = ROOT / "config/cicd/prepared-nyx"
PREPARED_NAMES = frozenset(
    {
        "ci.yaml",
        "landscape-long-run.yml",
        "codeql.yaml",
        "composer-redaction-gate.yml",
        "enforce-allowlist-judge-gates.yaml",
        "enforce-telemetry-backfill-trailer.yaml",
    }
)


def workflow_paths(directory: Path) -> list[Path]:
    return sorted(path for path in directory.iterdir() if path.suffix in {".yaml", ".yml"})


def all_workflow_paths(executable: Path = EXECUTABLE_WORKFLOWS, prepared: Path = PREPARED_WORKFLOWS) -> list[Path]:
    active_paths = workflow_paths(executable)
    prepared_paths = workflow_paths(prepared)
    assert active_paths, "executable workflow inventory is empty"
    assert {path.name for path in prepared_paths} == PREPARED_NAMES, "prepared inventory differs from reviewed package"
    assert {path.name for path in active_paths}.isdisjoint(path.name for path in prepared_paths), "duplicate workflow source identity"
    return sorted(active_paths + prepared_paths)

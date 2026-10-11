"""Enumerate the reviewed active workflow inventory without hiding duplicates."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EXECUTABLE_WORKFLOWS = ROOT / ".github/workflows"
WORKFLOW_NAMES = frozenset(
    {
        "build-push.yaml",
        "ci.yaml",
        "landscape-long-run.yml",
        "codeql.yaml",
        "composer-redaction-gate.yml",
        "enforce-allowlist-judge-gates.yaml",
        "enforce-telemetry-backfill-trailer.yaml",
        "mutation-testing.yaml",
        "pages.yaml",
        "state-engine-live-provider.yml",
    }
)


def workflow_paths(directory: Path) -> list[Path]:
    return sorted(path for path in directory.iterdir() if path.suffix in {".yaml", ".yml"})


def all_workflow_paths(executable: Path = EXECUTABLE_WORKFLOWS) -> list[Path]:
    active_paths = workflow_paths(executable)
    assert active_paths, "executable workflow inventory is empty"
    identities = [path.stem for path in active_paths]
    assert len(identities) == len(set(identities)), "duplicate workflow source identity"
    assert {path.name for path in active_paths} == WORKFLOW_NAMES, "active inventory differs from reviewed package"
    return active_paths

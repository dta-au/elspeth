"""Contracts for the GitHub Actions versions reviewed for release/0.7.2."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github/workflows"

EXPECTED_REVISIONS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/configure-pages": "45bfe0192ca1faeb007ade9deae92b16b8254a0d",
    "actions/deploy-pages": "cd2ce8fcbc39b97be8ca5fce6e763baed58fa128",
    "actions/setup-node": "820762786026740c76f36085b0efc47a31fe5020",
    "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
    "actions/upload-pages-artifact": "fc324d3547104276b827a68afc52ff2a11cc49c9",
    "astral-sh/setup-uv": "c771a70e6277c0a99b617c7a806ffedaca235ff9",
    "docker/build-push-action": "53b7df96c91f9c12dcc8a07bcb9ccacbed38856a",
    "docker/login-action": "af1e73f918a031802d376d3c8bbc3fe56130a9b0",
    "docker/metadata-action": "dc802804100637a589fabce1cb79ff13a1411302",
    "docker/setup-buildx-action": "bb05f3f5519dd87d3ba754cc423b652a5edd6d2c",
    "docker/setup-qemu-action": "96fe6ef7f33517b61c61be40b68a1882f3264fb8",
    "softprops/action-gh-release": "3d0d9888cb7fd7b750713d6e236d1fcb99157228",
}


def _workflow_steps() -> list[tuple[Path, dict[str, Any]]]:
    steps: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job in workflow.get("jobs", {}).values():
            steps.extend((path, step) for step in job.get("steps", ()) if isinstance(step, dict))
    return steps


def test_reviewed_actions_use_the_release_approved_revisions() -> None:
    seen: set[str] = set()
    for path, step in _workflow_steps():
        uses = str(step.get("uses", ""))
        action, separator, revision = uses.partition("@")
        if action not in EXPECTED_REVISIONS:
            continue
        seen.add(action)
        assert separator, f"{path}: {action} must pin an immutable revision"
        assert revision == EXPECTED_REVISIONS[action], f"{path}: unexpected {action} revision"

    assert seen == EXPECTED_REVISIONS.keys()


def test_setup_uv_v9_preserves_the_release_cache_policy_explicitly() -> None:
    setup_uv_steps = [(path, step) for path, step in _workflow_steps() if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")]
    assert setup_uv_steps

    for path, step in setup_uv_steps:
        inputs = step.get("with", {})
        assert inputs.get("enable-cache") is False, f"{path}: setup-uv caching must stay disabled"
        assert inputs.get("prune-cache") is True, f"{path}: setup-uv must keep pruning enabled"
